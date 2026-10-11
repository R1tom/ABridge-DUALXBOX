# PadBridge

A Windows companion app for **PS5 (DualSense)** and **Xbox** controllers on PC.

- **DualSense → Xbox:** games see your DualSense as a normal Xbox controller, with rumble, adaptive triggers and audio haptics.
- **Xbox controller:** games get a calibrated virtual copy, plus **impulse-trigger effects in every game**.
- **Per-game profiles** for both controllers, a **battery saver**, and very little added input delay.

![Xbox controller tab](docs/tab1.png)

## Features

### PS5 DualSense (USB or Bluetooth)
- Shows up as a virtual **Xbox 360 controller** through ViGEmBus, so every game that supports Xbox controllers works.
- **Rumble** from games, 0–200% strength.
- **Adaptive triggers:** each game can have its own L2/R2 effect: light or heavy resistance, gun "click", or machine-gun rattle.
  - **Simulated guns** (pistol, assault rifle, burst, SMG, machine gun, shotgun, sniper) kick the trigger at their rate of fire, with an optional recoil pulse. The mic button switches between guns.
  - For games with real PS5 support, tick **"supports the PS5 controller"** and the app hands the DualSense to the game.
- **Audio haptics (USB):** feel the game's sound through the haptic motors, with a noise gate so quiet music and ambience aren't felt.
- Lightbar colour and brightness, player-number LEDs, calibration wizard, firmware-updater shortcut.

### Xbox controller (Series X|S / One, Bluetooth)
- **Virtual copy** mode: games get a corrected copy with your calibration and deadzones applied.
- **Trigger motors (impulse triggers) in all games**, sent straight to the controller over Bluetooth HID. Per game, LT and RT can each be set to:
  - **Buzz with the game's rumble** while you hold the trigger
  - **Aim:** a soft click when you pull past halfway, then buzz with the game's rumble
  - **Guns:** the same seven presets as the DualSense
  - **Powers:** e.g. *boulder launch*, a launch thump followed by the game's own power surge, boosted
  - **Machine-gun rattle** or **off**

![Triggers tab](docs/tab2.png)

### Both controllers
- **Per-game trigger profiles.** The game is detected when it starts, and while the Triggers tab is open you can try a profile live.
- **Battery:**
  - Low-battery warning in the tray.
  - **Battery saver:** automatic below a chosen %, always on, or off. It halves rumble, dims the lightbar and keeps only short trigger effects.
  - Stuck rumble is stopped after 8 s if a game leaves it on and you're not touching the controller.
- **HidHide** integration: games only see the virtual controllers, so there's no double input. Quitting the app shows the real controllers again.
- Windows 11 style UI (light/dark), tray icon, start with Windows.

![Settings tab](docs/tab3.png)

## Latency
The input path does only three things: read the controller, apply calibration, update the virtual pad. Rumble, trigger effects, guns and battery checks run on other threads. Measured on an i5-11400F for the Xbox copy:
- Polls every **0.50 ms** (median; 99th percentile 0.57 ms)
- Forwarding takes **0.02 ms**

That's about **0.25 ms** added on average. The DualSense side is event-driven: it forwards each report as soon as it arrives.

## Requirements
- Windows 10/11
- Python 3.12
- [ViGEmBus](https://github.com/nefarius/ViGEmBus/releases) (virtual controllers)
- Optional: [HidHide](https://github.com/nefarius/HidHide/releases) (hides the real controllers from games)
- Xbox trigger motors need the controller connected over **Bluetooth**. Over USB or the Xbox Wireless Adapter you get normal rumble only.

## Install & run
```bat
git clone https://github.com/R1tom/PadBridge
cd PadBridge
py -3.12 -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\pythonw.exe ps5toxbox.pyw
```
Settings are saved in `config.json` next to the app, which is created on first run. `--minimized` starts the app in the tray, and `--tab N` opens it on a given tab.

## Notes
- Windows.Gaming.Input ignores vibration from background apps, and XInput has no trigger motors. That's why the Xbox trigger effects are written as Bluetooth HID output report `0x03` (`[mask, LT, RT, strong, weak, on-time, off-time, repeat]`).
- Not affiliated with Sony or Microsoft.

## License
[MIT](LICENSE)
