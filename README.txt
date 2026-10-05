Power Meter Live  v1.5
======================

A small Windows app that shows a live, rolling plot of the optical power read
from an Agilent / HP 8163A Lightwave Multimeter's power-meter module
(e.g. HP 81532A). It is the "Live power" panel from Lab Control as a
standalone program.


GETTING STARTED
---------------
1. Install Python 3.10 or newer from https://www.python.org/downloads/
   (tick "Add python.exe to PATH" in the installer).
2. Unzip this folder anywhere and double-click run.bat.
   The first run sets up a private Python environment (.venv) in the folder
   and downloads the packages; that takes a minute and needs internet.
   Later runs start straight away.
   It also puts "Power Meter Live" shortcuts (with the app's icon) in this
   folder and on the desktop; start the app from those from now on, or
   right-click one > Pin to taskbar / Pin to Start. If the folder moves,
   delete "Power Meter Live.lnk" here and run run.bat again.
3. Choose the connection, then press Start.

To try it without hardware:  run.bat --sim   (or pick "Simulated").

For GPIB you also need NI-VISA (or Keysight IO Libraries) and the GPIB
adapter's driver. RS-232 works without them.


CONNECTION
----------
RS-232     COM port, baud rate and flow control must match the 8163A
           (front panel: Config > RS232). The defaults are COM4, 9600 baud,
           no handshake. A null-modem cable is usually needed.
GPIB       Board (usually 0) and address (front panel: Config > GPIB;
           default 20).
VISA       Any VISA resource string, e.g. GPIB0::20::INSTR or ASRL4::INSTR.
Slot       The power-meter module's slot (default 2).
Laser slot The laser source's slot (default 1, e.g. HP 81689A); 0 = none.

On Start the meter is switched to continuous measurement (INIT<n>:CONT 1)
and FETC<n>:POW? is read every interval. Readings are converted to watts
whether the meter displays W or dBm. If the meter is in relative (dB) mode
there is no absolute power to plot; switch it to absolute on the front panel.


CONTROLS
--------
Scale            Plot units: pW, nW, uW, mW or W.
Keep N every M   Rolling window of the last N samples, one read every M ms.
Clear            Empties the window and restarts the time axis.
Export CSV       Saves time_s, power_W and unix_time for the samples kept.
Auto y-range     Off: use the fixed limits. "Fit to data" sets them to
                 min - mean/10 ... max + mean/10.
Goal             Draws a dashed horizontal line at that power.
Reference        Shows the reading as a percentage of this power (eta).

Statistics (mean, std dev, min, max) are over the samples in the window.
Settings are remembered between runs.


LASER
-----
With a laser slot set, a Laser button appears next to Start once connected:
"Laser off" (grey) or "Laser ON" (red). Click it to switch the laser output
(OUTP<n>:STAT, or SOUR<n>:POW:STAT on modules that only know that form);
switching on asks for confirmation first. The app waits until the module
reports the new state (up to 8 s) and shows the slot, model and wavelength
in the line under the connection settings. Switching from the front panel
is picked up within 2 s.
- Stop leaves the laser as it is. Closing the app with the laser on asks
  whether to switch it off first.
- If it won't switch on and the 8163A's lasers are locked, unlock them on
  the front panel (laser lock / password; factory password 1234).

TROUBLESHOOTING
---------------
"The meter did not switch to continuous measurement" lists what the meter
answered to INIT<n>:CONT? and its error queue (SYST:ERR?), then a likely cause:
- Settings conflict (-221): a logging / stability / MinMax function is
  running, or the trigger setup blocks it. Stop it on the front panel.
- Replies out of step / garbled: baud rate mismatch or a noisy link. Try a
  lower baud rate, or a hardware handshake on both sides.
- Reads back 0: another program is controlling the same meter (e.g. Lab
  Control or MATLAB over GPIB). Close it. A COM port can only be opened by
  one program at a time; if another program has it, you get "Could not open
  ASRL..." instead.
While running, the app checks every 2 s that continuous measurement is still
on, switches it back on if not, and says so under the plot.

Every setting command is followed by SYST:ERR? before the next one is sent:
the answer confirms the meter has taken the command in and whether it was
accepted, and a late reply is discarded so it can't answer the next question.
(*OPC? isn't used: with continuous measurement on it never answers, because
a continuous measurement is an operation that never completes.)

SINGLE-FILE .EXE (OPTIONAL)
---------------------------
After running run.bat once, double-click build_exe.bat to make
PowerMeterLive.exe (with the icon), which runs on PCs without Python
installed. It is copied to the folder above this one (..\PowerMeterLive.exe);
copy that one file to other PCs. The .exe contains a frozen copy of the code,
so run build_exe.bat again after any change to power_meter_live.py (close the
app first, or the old .exe can't be replaced).


FILES
-----
power_meter_live.py   the program (one file)
requirements.txt      Python packages it needs
run.bat               launcher; sets up .venv on the first run
build_exe.bat         optional single-file .exe build
assets\               the app icon (.ico for Windows, .png)
