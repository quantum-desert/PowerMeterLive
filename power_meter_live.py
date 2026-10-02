"""Power Meter Live: a standalone live plot for an Agilent / HP 8163A power meter.

A one-file version of the "Live power" card in Lab Control. It connects to an
8163A Lightwave Multimeter (RS-232, GPIB or any VISA resource), switches the
power-meter module to continuous measurement (INIT<n>:CONT 1) and reads
FETC<n>:POW? on a timer into a rolling window, with:

* Start / Stop, power scale pW ... W, keep the last N samples every M ms
* big readout (+ dBm), mean / std dev / min / max, samples, eta vs a reference,
  elapsed time and read rate
* auto or fixed y-range with "Fit to data", a goal line, Clear, Export CSV

Readings are converted to watts whether the meter displays W or dBm. In
relative (dB) mode there is no absolute power, so samples show as gaps.

Run:   python power_meter_live.py          (or run.bat on Windows)
       python power_meter_live.py --sim    (simulated meter, no hardware)
"""

from __future__ import annotations

import csv
import math
import os
import random
import re
import sys
import time
from collections import deque

import numpy as np
from PySide6.QtCore import QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox,
    QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)
import pyqtgraph as pg  # imported after PySide6 so pyqtgraph uses it

APP_NAME = "Power Meter Live"
VERSION = "1.4"

UNITS = [("pW", 1e12), ("nW", 1e9), ("µW", 1e6), ("mW", 1e3), ("W", 1.0)]
SCALE = dict(UNITS)
LINKS = [("serial", "RS-232"), ("gpib", "GPIB"), ("visa", "VISA resource"), ("sim", "Simulated")]
BAUDS = (9600, 19200, 38400, 57600, 115200)
FLOWS = [("none", "None"), ("rts_cts", "RTS/CTS"), ("xon_xoff", "XON/XOFF")]
OVERRANGE = 1e30            # larger readings are the meter's "no value" markers
MAX_FAILURES = 3            # reads failing in a row before the stream stops
LINE = "#2a78d6"

DEFAULTS = {
    "link": "serial", "com_port": "COM4", "baud": 9600, "flow": "none",
    "gpib_board": 0, "gpib_address": 20, "resource": "", "pm_slot": 2, "timeout_ms": 3000,
    "window": 500, "interval_ms": 150, "unit": "µW", "auto_y": True,
    "y_min": 0.0, "y_max": 50.0, "goal": None, "reference": None,
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def dbm_to_w(dbm: float) -> float:
    return 10 ** (dbm / 10) * 1e-3


def w_to_dbm(w: float) -> float:
    return 10 * math.log10(w / 1e-3) if w and w > 0 else float("-inf")


END_MARK = re.compile(r"\s*<END>\s*$", re.IGNORECASE)


def clean_reply(text) -> str:
    """Strip whitespace and the literal '<END>' the 8163A appends to every reply on
    RS-232 (it arrives as text, e.g. '1<END>' or '+1.23E-05<END>')."""
    return END_MARK.sub("", str(text)).strip()


def parse_float(text):
    try:
        return float(clean_reply(text).split(",")[0])
    except (TypeError, ValueError):
        return None


def fmt(v, digits: int = 4) -> str:
    return "—" if v is None or not math.isfinite(v) else f"{v:.{digits}g}"


def num(text: str):
    try:
        return float(text) if str(text).strip() else None
    except ValueError:
        return None


def resource_string(cfg: dict) -> str:
    link = cfg["link"]
    if link == "visa":
        return cfg["resource"].strip()
    if link == "gpib":
        return f"GPIB{int(cfg['gpib_board'])}::{int(cfg['gpib_address'])}::INSTR"
    if link == "sim":
        return "simulated 8163A"
    port = cfg["com_port"].strip()
    if port.upper().startswith("COM") and port[3:].isdigit():
        return f"ASRL{int(port[3:])}::INSTR"
    return port if "::" in port else f"ASRL{port}::INSTR"


def serial_ports() -> list[str]:
    try:
        from serial.tools import list_ports
        return sorted((p.device for p in list_ports.comports()),
                      key=lambda s: (len(s), s))
    except Exception:
        return []


# ---------------------------------------------------------------------------
# instrument
# ---------------------------------------------------------------------------

class SimMeter:
    """Stands in for a pyvisa resource: an 8163A with a power meter in every slot."""

    def __init__(self) -> None:
        self.t0 = time.time()
        self.cont = 0

    def write(self, cmd: str) -> None:
        c = cmd.upper()
        if c.startswith("INIT") and ":CONT" in c:
            self.cont = 1 if c.rstrip().endswith(("1", "ON")) else 0

    def query(self, cmd: str) -> str:
        time.sleep(0.004)
        c = cmd.upper()
        if c == "*IDN?":
            return "Agilent Technologies,8163A,SIMULATED,V4.20"
        if c == "*OPC?":
            return "1"
        if c.startswith("SYST:ERR"):
            return '+0,"No error"'
        if c.startswith("SLOT") and "EMPT" in c:
            return "+0"
        if c.startswith("SLOT") and "IDN" in c:
            return "HEWLETT-PACKARD,81532A,SIMULATED,V4.20"
        if c.startswith("INIT"):
            return f"+{self.cont}"
        if "UNIT?" in c:
            return "+1"
        if "REF:STAT" in c:
            return "+0"
        if "WAV?" in c:
            return "+1.55000000E-006"
        if c.startswith(("FETC", "READ")):
            t = time.time() - self.t0
            w = 20e-6 * (1 + 0.35 * math.sin(2 * math.pi * t / 30)) * (1 + random.gauss(0, 0.01))
            return f"{w:+.8E}"
        return "0"

    def clear(self) -> None:
        pass

    def close(self) -> None:
        pass


class Meter:
    """The 8163A power meter, the few commands the live plot needs."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.slot = int(cfg["pm_slot"])
        self.res = None
        self.unit = 1            # meter display unit: 1 = W, 0 = dBm
        self.relative = False    # reference state: relative readings are in dB

    def open(self) -> dict:
        cfg = self.cfg
        if cfg["link"] == "sim":
            self.res = SimMeter()
        else:
            self.res = self._open_visa(cfg)
        n = self.slot
        try:
            self._drain()                       # bytes left over from an earlier session
            try:
                idn = self.query("*IDN?")
            except Exception as exc:
                hint = ("Check the COM port, that the baud rate and handshake match the 8163A "
                        "(Config > RS232 on the front panel), and the cable (usually null-modem)."
                        if cfg["link"] == "serial" else
                        "Check the GPIB address (Config > GPIB on the front panel), the cable, "
                        "and that NI-VISA / the GPIB adapter driver is installed.")
                raise RuntimeError(f"No answer to *IDN? at {resource_string(cfg)}: {exc}\n\n{hint}")
            if "816" not in idn:
                raise RuntimeError(f"Expected an Agilent 8163A/8164A/8166A but got {idn!r}.")
            if (parse_float(self._opt(f"SLOT{n}:EMPT?")) or 0) == 1:
                raise RuntimeError(f"The 8163A reports slot {n} empty. Check 'Slot'.")
            module = (self._opt(f"SLOT{n}:IDN?") or "").strip()
            self.write("*CLS")
            self.set_continuous()
            self.read_unit()
            wav = parse_float(self._opt(f"SENS{n}:POW:WAV?"))
        except Exception:
            self.close()
            raise
        return {"idn": idn, "module": module, "wavelength_m": wav}

    # -- low-level I/O -------------------------------------------------------------
    def _drain(self, ms: int = 150) -> None:
        """Throw away anything waiting in the input buffer, including a reply that
        arrives late (after its query timed out). Without this every later reply
        is off by one, e.g. INIT2:CONT? would read the module's ID string."""
        try:
            from pyvisa import constants as C
            self.res.flush(C.BufferOperation.discard_read_buffer_no_io
                           | C.BufferOperation.discard_receive_buffer)
        except Exception:
            pass
        old = getattr(self.res, "timeout", None)
        try:
            self.res.timeout = ms
            for _ in range(20):
                try:
                    self.res.read_raw()
                except Exception:
                    break
        finally:
            if old is not None:
                self.res.timeout = old

    def _sync(self) -> str:
        """Ask SYST:ERR? after a setting command and return its reply.

        The meter reads commands in order, so the answer only comes once the
        command before it has been taken in; sending the next command only then
        keeps RS-232 without a handshake from overrunning the meter. The reply
        also says whether that command was accepted.

        *OPC? is NOT used for this: it waits until all pending operations are
        finished, and with continuous measurement on (INIT<n>:CONT 1) there is
        always one pending, so *OPC? never answers. On its own, with the
        meter idle, it answers 1 at once, as in the Lab Control terminal.
        """
        last = None
        for _ in range(3):
            try:
                reply = self.query("SYST:ERR?")
                if re.match(r"^[+-]?\d+\s*,", reply):
                    return reply
                last = f"unexpected reply {reply!r}"
            except Exception as exc:
                last = exc
            self._drain()
        raise RuntimeError(f"The meter did not answer SYST:ERR? after a command ({last}).")

    def query(self, cmd: str) -> str:
        """Send a query and return its reply without the trailing '<END>' marker."""
        return clean_reply(self.res.query(cmd))

    def write(self, cmd: str) -> str:
        """A setting command, then SYST:ERR? before anything else is sent.
        Returns the error reply ('' when the command was accepted)."""
        self.res.write(cmd)
        err = self._sync()
        return "" if re.match(r"^[+]?0\s*,", err) else err

    def errors(self) -> list[str]:
        """Empty the meter's error queue (SYST:ERR?)."""
        out = []
        for _ in range(10):
            e = (self._opt("SYST:ERR?") or "").strip()
            if not e or e.startswith(("0", "+0")):
                break
            out.append(e)
        return out

    def continuous(self):
        """INIT<n>:CONT? as 0/1, or None if the reply isn't one (out of step)."""
        reply = self._opt(f"INIT{self.slot}:CONT?")
        v = parse_float(reply)
        return int(v) if v in (0, 1) else None, reply

    def set_continuous(self) -> None:
        """INIT<n>:CONT 1, verified as in the MATLAB app; retried after resynchronising."""
        n = self.slot
        seen, errs = [], []
        for _ in range(3):
            err = self.write(f"INIT{n}:CONT 1")
            if err:
                errs.append(err)
            state, reply = self.continuous()
            if state == 1:
                return
            seen.append(reply)
            errs += self.errors()
            self._drain()
        why = ""
        if any("221" in e for e in errs):
            why = ("The meter refuses it with a settings conflict: a logging, stability or "
                   "MinMax function may be running, or the trigger setup blocks it. Stop "
                   "it on the front panel (or press Preset) and try again.")
        elif any(r is None or parse_float(r) not in (0, 1) for r in seen):
            why = ("The replies don't belong to the question (the link is out of step or "
                   "garbled). Check that the baud rate matches the meter, try a lower baud "
                   "rate, or turn on a hardware handshake on both sides if the cable has it.")
        else:
            why = ("The meter accepts the command but reads back 0. Another program "
                   "controlling the same meter (e.g. Lab Control or MATLAB over GPIB) can "
                   "switch it back off; close it and try again.")
        raise RuntimeError(
            f"The meter did not switch to continuous measurement.\n\n"
            f"INIT{n}:CONT? replies: {', '.join(repr(r) for r in seen)}\n"
            f"Meter errors: {'; '.join(errs) or 'none'}\n\n{why}")

    @staticmethod
    def _open_visa(cfg: dict):
        import pyvisa
        from pyvisa import constants as C
        try:
            rm = pyvisa.ResourceManager()        # NI-VISA / Keysight VISA if installed
        except Exception:
            rm = pyvisa.ResourceManager("@py")   # pure-Python fallback (RS-232 / LAN)
        name = resource_string(cfg)
        if not name:
            raise RuntimeError("Enter a VISA resource, e.g. GPIB0::20::INSTR")
        try:
            res = rm.open_resource(name)
        except Exception as exc:
            raise RuntimeError(f"Could not open {name}: {exc}")
        res.timeout = int(cfg["timeout_ms"])
        res.read_termination = "\n"
        res.write_termination = "\n"
        if cfg["link"] == "serial":
            res.baud_rate = int(cfg["baud"])
            res.data_bits = 8
            res.parity = C.Parity.none
            res.stop_bits = C.StopBits.one
            res.flow_control = {"none": C.ControlFlow.none, "rts_cts": C.ControlFlow.rts_cts,
                                "xon_xoff": C.ControlFlow.xon_xoff}[cfg["flow"]]
        return res

    def _opt(self, cmd: str):
        """A query some modules don't answer; None instead of an error."""
        try:
            return self.query(cmd)
        except Exception:
            self._drain()          # its reply may still arrive; don't let it answer the next query
            return None

    def read_unit(self) -> None:
        n = self.slot
        u = parse_float(self._opt(f"SENS{n}:POW:UNIT?"))
        self.unit = 1 if u is None else int(u)
        self.relative = bool(parse_float(self._opt(f"SENS{n}:POW:REF:STAT?")) or 0)

    def read_power(self) -> tuple[float, float]:
        """(unix time, watts); watts is NaN when there's no absolute reading."""
        try:
            reply = self.query(f"FETC{self.slot}:POW?")
        except Exception:
            self._drain()
            raise
        raw = parse_float(reply)
        t = time.time()
        if raw is None:            # not a number: out of step with the meter
            self._drain()
            raise RuntimeError(f"Unexpected reply to FETC{self.slot}:POW?: {reply!r}")
        if raw is None or not math.isfinite(raw) or abs(raw) >= OVERRANGE or self.relative:
            return t, float("nan")
        return t, (raw if self.unit == 1 else dbm_to_w(raw))

    def close(self) -> None:
        if self.res is not None:
            try:
                self.res.close()
            except Exception:
                pass
            self.res = None


class Reader(QThread):
    """Connects, then reads the meter every interval until stopped (off the GUI thread)."""

    connected = Signal(dict)       # idn, module, wavelength_m
    meter_mode = Signal(int, bool)  # display unit (1 W / 0 dBm), relative
    sample = Signal(float, float)  # unix time, watts
    warning = Signal(str)
    failed = Signal(str)

    def __init__(self, cfg: dict, interval_ms: int) -> None:
        super().__init__()
        self.cfg = dict(cfg)
        self.interval = max(20, interval_ms) / 1000
        self._stop = False

    def set_interval(self, ms: int) -> None:
        self.interval = max(20, ms) / 1000

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        meter = Meter(self.cfg)
        try:
            info = meter.open()
            if self._stop:
                return
            self.connected.emit(info)
            self.meter_mode.emit(meter.unit, meter.relative)
            failures = 0
            next_mode = time.monotonic() + 2.0
            next_read = time.monotonic()
            while not self._stop:
                try:
                    t, w = meter.read_power()
                    failures = 0
                    self.sample.emit(t, w)
                except Exception as exc:
                    failures += 1
                    if failures >= MAX_FAILURES:
                        raise RuntimeError(f"{MAX_FAILURES} reads failed in a row: {exc}")
                if time.monotonic() >= next_mode:      # pick up front-panel unit changes
                    unit, rel = meter.unit, meter.relative
                    meter.read_unit()
                    if (unit, rel) != (meter.unit, meter.relative):
                        self.meter_mode.emit(meter.unit, meter.relative)
                    if meter.continuous()[0] == 0:      # switched off behind our back
                        self.warning.emit(
                            "Continuous measurement was switched off by something else "
                            "(another program on the meter, e.g. Lab Control or MATLAB over "
                            "GPIB, or the front panel), so it was switched back on. Readings "
                            "may repeat while it is off.")
                        try:
                            meter.set_continuous()
                        except Exception as exc:
                            self.warning.emit(str(exc))
                    next_mode = time.monotonic() + 2.0
                next_read += self.interval
                now = time.monotonic()
                if next_read < now:                    # fell behind: don't burst to catch up
                    next_read = now
                while not self._stop and time.monotonic() < next_read:
                    time.sleep(min(0.02, max(0.0, next_read - time.monotonic())))
        except Exception as exc:
            if not self._stop:
                self.failed.emit(str(exc))
        finally:
            meter.close()


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

def small(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setStyleSheet("color: #6b7280;")
    return lab


def ispin(lo: int, hi: int, width: int = 70) -> QSpinBox:
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
    s.setMinimumWidth(width)
    return s


def dspin() -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(-1e15, 1e15)
    s.setDecimals(4)
    s.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
    s.setKeyboardTracking(False)
    s.setFixedWidth(110)
    return s


def row(*widgets, spacing: int = 8) -> QHBoxLayout:
    lay = QHBoxLayout()
    lay.setSpacing(spacing)
    for w in widgets:
        if w is None:
            lay.addStretch(1)
        elif isinstance(w, int):
            lay.addSpacing(w)
        else:
            lay.addWidget(w)
    return lay


class Stat(QWidget):
    def __init__(self, title: str) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(1)
        lay.addWidget(small(title))
        self.value = QLabel("—")
        f = self.value.font()
        f.setPointSize(13)
        self.value.setFont(f)
        lay.addWidget(self.value)
        self.setMinimumWidth(110)

    def set(self, text: str) -> None:
        self.value.setText(text)


class MainWindow(QMainWindow):
    def __init__(self, simulate: bool = False) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} · Agilent 8163A")
        self.settings = QSettings("PowerMeterLive", "PowerMeterLive")
        self.cfg = self._load_cfg()
        if simulate:
            self.cfg["link"] = "sim"

        n = int(self.cfg["window"])
        self.t: deque[float] = deque(maxlen=n)
        self.w: deque[float] = deque(maxlen=n)
        self.t0: float | None = None
        self.reader: Reader | None = None
        self.running = False
        self.dirty = True
        self.failures_msg = ""

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)

        # -- connection ---------------------------------------------------------------
        self.link = QComboBox()
        for key, name in LINKS:
            self.link.addItem(name, key)
        self.com = QComboBox()
        self.com.setEditable(True)
        self.com.setMinimumWidth(90)
        self.rescan = QPushButton("↻")
        self.rescan.setFixedWidth(28)
        self.rescan.setToolTip("Rescan COM ports")
        self.rescan.clicked.connect(self._fill_ports)
        self.baud = QComboBox()
        for b in BAUDS:
            self.baud.addItem(str(b), b)
        self.flow = QComboBox()
        for key, name in FLOWS:
            self.flow.addItem(name, key)
        self.board = ispin(0, 9, 40)
        self.addr = ispin(0, 30, 40)
        self.resource = QLineEdit()
        self.resource.setPlaceholderText("GPIB0::20::INSTR")
        self.resource.setMinimumWidth(200)
        self.slot = ispin(1, 4, 36)
        self.timeout = ispin(200, 60000, 60)
        self.timeout.setSuffix(" ms")
        self.start_btn = QPushButton("Start")
        self.start_btn.setMinimumWidth(100)
        self.start_btn.clicked.connect(self.toggle)
        self.state = QLabel()
        self.state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state.setMinimumWidth(90)

        self.serial_w = [small("Port"), self.com, self.rescan, small("Baud"), self.baud,
                         small("Flow"), self.flow]
        self.gpib_w = [small("Board"), self.board, small("Address"), self.addr]
        self.visa_w = [small("Resource"), self.resource]
        self.conn_w = ([self.link] + self.serial_w + self.gpib_w + self.visa_w
                       + [self.slot, self.timeout])
        root.addLayout(row(small("Connection"), self.link, 6, *self.serial_w, *self.gpib_w,
                           *self.visa_w, 6, small("Slot"), self.slot, small("Timeout"),
                           self.timeout, None, self.state, self.start_btn))
        self.meter_info = small("")
        root.addWidget(self.meter_info)
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: #e5e7eb;")
        root.addWidget(line)

        # -- readout + statistics ------------------------------------------------------
        self.now = QLabel("—")
        f = QFont(self.now.font())
        f.setPointSize(26)
        f.setWeight(QFont.Weight.DemiBold)
        self.now.setFont(f)
        self.now.setMinimumWidth(220)
        self.now_sub = small("")
        big = QVBoxLayout()
        big.setSpacing(0)
        big.addWidget(self.now)
        big.addWidget(self.now_sub)
        stats = QGridLayout()
        stats.setHorizontalSpacing(18)
        stats.setVerticalSpacing(6)
        self.st = {}
        for i, (key, title) in enumerate([
                ("mean", "Mean"), ("std", "Std dev"), ("min", "Min"), ("max", "Max"),
                ("n", "Samples"), ("eta", "η vs reference"), ("elapsed", "Elapsed"),
                ("rate", "Rate")]):
            self.st[key] = Stat(title)
            stats.addWidget(self.st[key], i // 4, i % 4)
        top = QHBoxLayout()
        top.addLayout(big)
        top.addSpacing(24)
        top.addLayout(stats)
        top.addStretch(1)
        root.addLayout(top)

        # -- plot -------------------------------------------------------------------------
        self.plot = pg.PlotWidget()
        self.plot.setBackground("w")
        self.plot.setMenuEnabled(False)
        self.plot.setMinimumHeight(260)
        pi = self.plot.getPlotItem()
        pi.showGrid(x=True, y=True, alpha=0.15)
        for ax in ("left", "bottom"):
            a = pi.getAxis(ax)
            a.setPen(pg.mkPen("#9ca3af"))
            a.setTextPen(pg.mkPen("#4b5563"))
            a.enableAutoSIPrefix(False)
        pi.setLabel("bottom", "time (s)", color="#4b5563")
        self.curve = pi.plot([], [], pen=pg.mkPen(LINE, width=2))
        self.goal_line = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen("#111827", width=1.5, style=Qt.PenStyle.DashLine),
            label="goal {value:.4g}",
            labelOpts={"position": 0.04, "color": "#4b5563", "fill": pg.mkBrush(255, 255, 255, 220)})
        self.goal_line.setVisible(False)
        pi.addItem(self.goal_line, ignoreBounds=True)
        self.msg = pg.TextItem("", color="#6b7280", anchor=(0.5, 0.5))
        pi.addItem(self.msg, ignoreBounds=True)
        root.addWidget(self.plot, 1)

        # -- controls ---------------------------------------------------------------------
        self.unit_group = QButtonGroup(self)
        unit_btns = []
        for name, _ in UNITS:
            b = QPushButton(name)
            b.setCheckable(True)
            b.setFixedWidth(42)
            b.setProperty("unit", name)
            self.unit_group.addButton(b)
            unit_btns.append(b)
        self.unit_group.setExclusive(True)
        self.unit_group.buttonClicked.connect(self._unit_changed)
        self.window_spin = ispin(2, 1_000_000, 110)
        self.window_spin.setSuffix(" samples")
        self.window_spin.editingFinished.connect(self._window_changed)
        self.interval_spin = ispin(20, 60_000, 80)
        self.interval_spin.setSuffix(" ms")
        self.interval_spin.editingFinished.connect(self._interval_changed)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.clear)
        export_btn = QPushButton("Export CSV…")
        export_btn.clicked.connect(self.export_csv)
        self.auto_y = QCheckBox("Auto y-range")
        self.auto_y.toggled.connect(self._y_changed)
        self.ymin, self.ymax = dspin(), dspin()
        for s in (self.ymin, self.ymax):
            s.valueChanged.connect(self._y_changed)
        fit_btn = QPushButton("Fit to data")
        fit_btn.setToolTip("min − mean/10 … max + mean/10")
        fit_btn.clicked.connect(self._fit_y)
        self.goal = QLineEdit()
        self.goal.setFixedWidth(110)
        self.goal.editingFinished.connect(self._goal_changed)
        self.ref = QLineEdit()
        self.ref.setFixedWidth(110)
        self.ref.setToolTip("η = reading / reference × 100 %")
        self.ref.editingFinished.connect(self._ref_changed)
        self.unit_hint = small("")

        root.addLayout(row(small("Scale"), *unit_btns, 18, small("Keep"), self.window_spin,
                           small("every"), self.interval_spin, 18, clear_btn, export_btn,
                           None, self.unit_hint, spacing=4))
        root.addLayout(row(self.auto_y, self.ymin, small("to"), self.ymax, fit_btn, 18,
                           small("Goal"), self.goal, 12, small("Reference"), self.ref, None))
        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: #b42318;")
        self.note.setVisible(False)
        root.addWidget(self.note)

        # -- restore state ------------------------------------------------------------
        c = self.cfg
        self.link.setCurrentIndex(max(0, self.link.findData(c["link"])))
        self._fill_ports()
        self.com.setCurrentText(c["com_port"])
        self.baud.setCurrentIndex(max(0, self.baud.findData(int(c["baud"]))))
        self.flow.setCurrentIndex(max(0, self.flow.findData(c["flow"])))
        self.board.setValue(int(c["gpib_board"]))
        self.addr.setValue(int(c["gpib_address"]))
        self.resource.setText(c["resource"])
        self.slot.setValue(int(c["pm_slot"]))
        self.timeout.setValue(int(c["timeout_ms"]))
        self.window_spin.setValue(int(c["window"]))
        self.interval_spin.setValue(int(c["interval_ms"]))
        for b in unit_btns:
            b.setChecked(b.property("unit") == c["unit"])
        self.auto_y.blockSignals(True)
        self.auto_y.setChecked(bool(c["auto_y"]))
        self.auto_y.blockSignals(False)
        self._set_y_spins(float(c["y_min"]), float(c["y_max"]))
        self.link.currentIndexChanged.connect(self._link_changed)
        self._link_changed()
        self._apply_unit_text()

        self._redraw_timer = QTimer(self)        # coalesce samples into ~20 redraws/s
        self._redraw_timer.timeout.connect(self._maybe_redraw)
        self._redraw_timer.start(50)
        self._clock = QTimer(self)
        self._clock.timeout.connect(self._tick_clock)
        self._clock.start(1000)
        self._set_state("Stopped", "#6b7280", "#f3f4f6")
        self.redraw()
        self.resize(1180, 760)

    # -- settings -------------------------------------------------------------------
    def _load_cfg(self) -> dict:
        cfg = dict(DEFAULTS)
        for k, default in DEFAULTS.items():
            v = self.settings.value(k, None)
            if v is None or v == "":
                continue
            try:
                if isinstance(default, bool):
                    cfg[k] = v if isinstance(v, bool) else str(v).lower() == "true"
                elif isinstance(default, int):
                    cfg[k] = int(v)
                elif isinstance(default, float) or k in ("goal", "reference"):
                    cfg[k] = None if str(v) == "none" else float(v)
                else:
                    cfg[k] = str(v)
            except (TypeError, ValueError):
                pass
        if cfg["unit"] not in SCALE:
            cfg["unit"] = "µW"
        return cfg

    def _save_cfg(self) -> None:
        self._read_conn_fields()
        for k, v in self.cfg.items():
            self.settings.setValue(k, "none" if v is None else v)

    def _read_conn_fields(self) -> None:
        c = self.cfg
        c["link"] = self.link.currentData()
        c["com_port"] = self.com.currentText().strip() or "COM4"
        c["baud"] = int(self.baud.currentData())
        c["flow"] = self.flow.currentData()
        c["gpib_board"] = self.board.value()
        c["gpib_address"] = self.addr.value()
        c["resource"] = self.resource.text().strip()
        c["pm_slot"] = self.slot.value()
        c["timeout_ms"] = self.timeout.value()

    def _fill_ports(self) -> None:
        current = self.com.currentText() or self.cfg["com_port"]
        self.com.clear()
        self.com.addItems(serial_ports())
        self.com.setCurrentText(current)

    def _link_changed(self) -> None:
        link = self.link.currentData()
        for group, key in ((self.serial_w, "serial"), (self.gpib_w, "gpib"),
                           (self.visa_w, "visa")):
            for w in group:
                w.setVisible(link == key)

    # -- start / stop ------------------------------------------------------------------
    def toggle(self) -> None:
        self.stop() if self.reader is not None else self.start()

    def start(self) -> None:
        self._read_conn_fields()
        self._save_cfg()
        self.failures_msg = ""
        self._show_note("")
        self.reader = Reader(self.cfg, self.interval_spin.value())
        self.reader.connected.connect(self._on_connected)
        self.reader.meter_mode.connect(self._on_mode)
        self.reader.sample.connect(self._on_sample)
        self.reader.failed.connect(self._on_failed)
        self.reader.warning.connect(self._show_note)
        self.reader.finished.connect(self._on_finished)
        for w in self.conn_w:
            w.setEnabled(False)
        self.start_btn.setText("Stop")
        self._set_state("Connecting…", "#1d4ed8", "#dbeafe")
        self.meter_info.setText(f"Connecting to {resource_string(self.cfg)}…")
        self.reader.start()
        self.redraw()

    def stop(self) -> None:
        if self.reader is not None:
            self.reader.stop()
            self.start_btn.setEnabled(False)
            self._set_state("Stopping…", "#6b7280", "#f3f4f6")

    def _on_connected(self, info: dict) -> None:
        self.running = True
        if self.t0 is None:
            self.t0 = time.time()
        wav = info.get("wavelength_m")
        self.meter_info.setText(
            f"{info['idn']}   ·   slot {self.cfg['pm_slot']}: {info['module'] or '—'}"
            + (f"   ·   λ {wav * 1e9:.1f} nm" if wav else "")
            + f"   ·   {resource_string(self.cfg)}")
        self._set_state("Live", "#067647", "#dcfae6")
        self.redraw()

    def _on_mode(self, unit: int, relative: bool) -> None:
        self.unit_hint.setText(f"meter shows {'W' if unit == 1 else 'dBm'}"
                               + (" · relative (dB)" if relative else ""))
        self._show_note("The meter is in relative (dB) mode, so readings can't be shown in "
                        "watts. Switch it to absolute on the front panel." if relative else "")

    def _on_sample(self, t: float, w: float) -> None:
        self.t.append(t)
        self.w.append(w)
        self.dirty = True

    def _on_failed(self, msg: str) -> None:
        self.failures_msg = msg

    def _on_finished(self) -> None:
        self.reader = None
        self.running = False
        for w in self.conn_w:
            w.setEnabled(True)
        self._link_changed()
        self.start_btn.setEnabled(True)
        self.start_btn.setText("Start")
        if self.failures_msg:
            self._set_state("Error", "#b42318", "#fee4e2")
            self._show_note(self.failures_msg)
            if not self.t:
                self.meter_info.setText("Not connected")
        else:
            self._set_state("Stopped", "#6b7280", "#f3f4f6")
        self.redraw()

    def _set_state(self, text: str, fg: str, bg: str) -> None:
        self.state.setText(text)
        self.state.setStyleSheet(f"color: {fg}; background: {bg}; border-radius: 10px; "
                                 f"padding: 3px 10px; font-weight: 600;")

    def _show_note(self, text: str) -> None:
        self.note.setText(text)
        self.note.setVisible(bool(text))

    # -- controls -----------------------------------------------------------------------
    @property
    def unit(self) -> str:
        b = self.unit_group.checkedButton()
        return b.property("unit") if b else "µW"

    @property
    def factor(self) -> float:
        return SCALE[self.unit]

    def _set_y_spins(self, lo: float, hi: float) -> None:
        for s, v in ((self.ymin, lo), (self.ymax, hi)):
            s.blockSignals(True)
            s.setValue(v)
            s.blockSignals(False)
            s.setEnabled(not self.auto_y.isChecked())

    def _apply_unit_text(self) -> None:
        u = self.unit
        self.plot.getPlotItem().setLabel("left", f"Power ({u})", color="#4b5563")
        for s in (self.ymin, self.ymax):
            s.setSuffix(f" {u}")
        self.goal.setPlaceholderText(f"none ({u})")
        self.ref.setPlaceholderText(f"none ({u})")
        g, r = self.cfg["goal"], self.cfg["reference"]
        self.goal.setText("" if g is None else f"{g * self.factor:.6g}")
        self.ref.setText("" if r is None else f"{r * self.factor:.6g}")
        self._goal_line()

    def _unit_changed(self, *_) -> None:
        old = self.cfg["unit"]
        k = SCALE[self.unit] / SCALE[old]          # keep a fixed y-range on the same power
        self.cfg["unit"] = self.unit
        self.cfg["y_min"], self.cfg["y_max"] = self.ymin.value() * k, self.ymax.value() * k
        self._set_y_spins(self.cfg["y_min"], self.cfg["y_max"])
        self._apply_unit_text()
        self.redraw()

    def _window_changed(self) -> None:
        n = max(2, self.window_spin.value())
        self.cfg["window"] = n
        self.t = deque(list(self.t)[-n:], maxlen=n)
        self.w = deque(list(self.w)[-n:], maxlen=n)
        self.redraw()

    def _interval_changed(self) -> None:
        self.cfg["interval_ms"] = self.interval_spin.value()
        if self.reader is not None:
            self.reader.set_interval(self.cfg["interval_ms"])

    def _y_changed(self, *_) -> None:
        self.cfg["auto_y"] = self.auto_y.isChecked()
        self.cfg["y_min"], self.cfg["y_max"] = self.ymin.value(), self.ymax.value()
        for s in (self.ymin, self.ymax):
            s.setEnabled(not self.auto_y.isChecked())
        self.redraw()

    def _fit_y(self) -> None:
        st = self.stats()
        if not math.isfinite(st["mean"]):
            return
        f = self.factor
        pad = abs(st["mean"]) * f / 10 or 1.0
        self.auto_y.blockSignals(True)
        self.auto_y.setChecked(False)
        self.auto_y.blockSignals(False)
        self._set_y_spins(max(0.0, st["min"] * f - pad), st["max"] * f + pad)
        self._y_changed()

    def _goal_changed(self) -> None:
        v = num(self.goal.text())
        self.cfg["goal"] = None if v is None else v / self.factor
        self._goal_line()
        self.redraw()

    def _ref_changed(self) -> None:
        v = num(self.ref.text())
        self.cfg["reference"] = None if not v else v / self.factor
        self.redraw()

    def _goal_line(self) -> None:
        g = self.cfg["goal"]
        self.goal_line.setVisible(g is not None)
        if g is not None:
            self.goal_line.setValue(g * self.factor)

    def clear(self) -> None:
        self.t.clear()
        self.w.clear()
        self.t0 = time.time() if self.running else None
        self.redraw()

    def export_csv(self) -> None:
        if not self.t:
            QMessageBox.information(self, APP_NAME, "There are no samples to export yet.")
            return
        default = os.path.join(os.path.expanduser("~"),
                               f"power_{time.strftime('%Y%m%d_%H%M%S')}.csv")
        path, _ = QFileDialog.getSaveFileName(self, "Export power samples", default, "CSV (*.csv)")
        if not path:
            return
        t, w = self.arrays()
        with open(path, "w", newline="", encoding="utf-8") as fh:
            out = csv.writer(fh)
            out.writerow([f"# Agilent 8163A slot {self.cfg['pm_slot']}, "
                          f"{resource_string(self.cfg)}, exported {time.strftime('%Y-%m-%d %H:%M:%S')}"])
            out.writerow(["time_s", "power_W", "unix_time"])
            for ti, wi, ui in zip(t, w, self.t):
                out.writerow([f"{ti:.4f}", f"{wi:.9e}", f"{ui:.3f}"])
        self.statusBar().showMessage(f"Exported {t.size} samples to {path}", 8000)

    # -- data + drawing -------------------------------------------------------------------
    def arrays(self) -> tuple[np.ndarray, np.ndarray]:
        t = np.fromiter(self.t, float, len(self.t))
        w = np.fromiter(self.w, float, len(self.w))
        return t - (self.t0 or (t[0] if t.size else 0.0)), w

    def stats(self) -> dict:
        _, w = self.arrays()
        good = w[np.isfinite(w)]
        nan = float("nan")
        if not good.size:
            return {"n": int(w.size), "last": nan, "mean": nan, "std": nan, "min": nan, "max": nan}
        return {"n": int(w.size), "last": float(w[-1]) if math.isfinite(w[-1]) else nan,
                "mean": float(good.mean()), "std": float(good.std(ddof=1)) if good.size > 1 else 0.0,
                "min": float(good.min()), "max": float(good.max())}

    def _maybe_redraw(self) -> None:
        if self.dirty:
            self.redraw()

    def redraw(self) -> None:
        self.dirty = False
        t, w = self.arrays()
        f, u = self.factor, self.unit
        y = w * f
        self.curve.setData(t, y, connect="finite")
        vb = self.plot.getPlotItem().getViewBox()
        if t.size:
            vb.setXRange(float(t[0]), float(max(t[-1], t[0] + 1e-3)), padding=0.02)
        if self.auto_y.isChecked() or not y.size:
            vb.enableAutoRange(axis="y")
        else:
            vb.setYRange(self.ymin.value(), self.ymax.value(), padding=0)

        st = self.stats()
        last = st["last"]
        self.now.setText(f"{fmt(last * f, 5)} {u}" if math.isfinite(last) else "—")
        self.now_sub.setText(f"{w_to_dbm(last):+.3f} dBm" if math.isfinite(last) and last > 0 else "")
        for key, d in (("mean", 4), ("std", 3), ("min", 4), ("max", 4)):
            v = st[key]
            self.st[key].set(f"{fmt(v * f, d)} {u}" if math.isfinite(v) else "—")
        self.st["n"].set(f"{st['n']} / {self.cfg['window']}")
        ref = self.cfg["reference"]
        self.st["eta"].set(f"{100 * last / ref:.2f} %" if ref and math.isfinite(last) else "—")
        if t.size > 2:
            dt = float(np.mean(np.diff(t[-50:])))
            self.st["rate"].set(f"{1 / dt:.1f} /s" if dt > 0 else "—")
        else:
            self.st["rate"].set("—")
        self._tick_clock()

        if t.size:
            self.msg.setText("")
        else:
            self.msg.setText("Waiting for the first reading…" if self.reader is not None
                             else "Press Start to read the power meter")
            (x0, x1), (y0, y1) = vb.viewRange()
            self.msg.setPos((x0 + x1) / 2, (y0 + y1) / 2)

    def _tick_clock(self) -> None:
        if self.t0 is None:
            s = 0.0
        elif self.running:
            s = time.time() - self.t0
        else:
            s = (self.t[-1] - self.t0) if self.t else 0.0
        self.st["elapsed"].set(time.strftime("%H:%M:%S", time.gmtime(s)) if s > 0 else "—")

    def closeEvent(self, event) -> None:
        self._save_cfg()
        if self.reader is not None:
            self.reader.stop()
            self.reader.wait(int(self.cfg["timeout_ms"]) + 2000)
        super().closeEvent(event)


def asset(name: str) -> str:
    """Path of a bundled file, next to this script or inside a PyInstaller .exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", name)


def main() -> int:
    if sys.platform == "win32":
        # Own taskbar identity, so Windows shows this app's icon rather than Python's
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Lab.PowerMeterLive")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    icon = QIcon(asset("power_meter_live.ico"))
    if icon.isNull():
        icon = QIcon(asset("power_meter_live.png"))
    app.setWindowIcon(icon)
    pg.setConfigOptions(antialias=True)
    win = MainWindow(simulate="--sim" in sys.argv)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
