<p align="center"><img src="frontend/logo.svg" width="90" alt=""></p>
<h1 align="center">Irodori 彩り</h1>

<p align="center"><b>Turn a colored 3D model into a multicolor print on a toolchanger 3D printer.</b><br>
Runs on your own computer. No account, and your models are never uploaded.</p>

![The Irodori window with a helmet model and suggested spool sets](docs/screenshot.png)

<sub>Model: "Damaged Helmet" by theblueturtle_, CC BY 4.0. It is not included in this repository.</sub>

## Who it is for

Irodori is made for **toolchanger printers**, where each color has its own toolhead. Mixing colors layer by layer means changing filament very often, and a toolchanger can do that without unloading and reloading a single nozzle. Printers with one nozzle and a filament changer (AMS, MMU) are not the target.

| Printer | Status |
|---|---|
| Snapmaker U1 | ready-made export, tested in Snapmaker Orca |
| Other toolchangers | the color analysis and preview work, but the export needs a profile for your printer. [Tell us which one you have](https://github.com/Devoane/irodori/issues) |

## Languages

English is the main language and what the app starts in. Change it in the top right corner of the app, and your choice is remembered.

| Language | In the app |
|---|---|
| English (default) | English |
| Polish | Polski |
| Spanish | Español |
| German | Deutsch |
| French | Français |
| Russian | Русский |
| Japanese | 日本語 |

## What it does

You drop in a colored model. Irodori then:

1. looks at which colors the model has,
2. picks 2–4 spools that can produce them,
3. arranges thin layers of different spools so that from a distance they look like a new color,
4. shows you what the print should look like,
5. saves a ready project for Snapmaker Orca, or G-code for the printer.

## What you need

| What | Why | Required? |
|---|---|---|
| [Python 3.11 or newer](https://www.python.org/downloads/) | runs the program | **yes** |
| Internet on the first start | one download of about 400 MB of libraries | **yes**, only once |
| A web browser | the program's window opens in it | **yes** |
| Snapmaker Orca 2.4 | opens the 3MF project or prepares the G-code | **yes**, to print |
| A toolchanger printer | prints the project. The ready-made export is for the Snapmaker U1 | **yes**, to print |
| [Blender](https://www.blender.org/download/) | only for FBX files | no |

## Install it step by step

### 1. Download the program

At the top of this page click the green **Code** button, then **Download ZIP**. Unpack the file, for example to your Desktop.

### 2. Install Python

Download it from [python.org](https://www.python.org/downloads/) and install it.

> **Windows:** on the first installer screen tick **Add python.exe to PATH**. Missing this is the most common cause of problems.

Linux usually has Python already. Check with `python3 --version`.

### 3. Start Irodori

| System | What to do |
|---|---|
| **Windows** | double-click `Irodori.bat` |
| **macOS** | right-click `Irodori.command`, choose **Open** and confirm |
| **Linux** | open a terminal in the program folder and type `./run.sh` |

The first start takes a few minutes because it downloads the libraries. Do not close the window, even if nothing seems to happen for a while. Your browser then opens by itself. If it does not, go to **http://127.0.0.1:8765/**.

Keep the terminal window open while you work.

## Your first print

1. **Load a model.** Drag the file onto **Drop a model or click**. GLB, OBJ with MTL, FBX with textures and painted 3MF all work. If the model needs more files, the program asks for them.
2. **Click Analyze.** At the default accuracy a large model can take a few minutes.
3. **Choose a set on the right.** The percentage says how close the result is to the model's colors. Higher is better. The set you click appears in the preview.
4. **Load the matching spools.** The names and colors are on the set's card. F1 is toolhead 1, F2 is toolhead 2, and so on.
5. **Get the file.** Click **3MF** and open it in Snapmaker Orca, or click **G-code** to get a file for the printer right away.
6. **Print.** Start with a small model.

Own some spools already? Enter them under **My filaments**, and the program adds a separate result for the spools in your printer. All the settings are explained in the program under **Help**.

## How to close it

Close the terminal window or press `Ctrl+C` in it. On Linux you can also run `./run.sh --stop`.

## When something does not work

| Problem | Fix |
|---|---|
| `python` is not recognized, or the window closes at once | Reinstall Python and tick **Add python.exe to PATH** |
| The first start seems stuck | Wait. The libraries are about 400 MB. Check your internet |
| The browser does not open | Go to http://127.0.0.1:8765/ yourself |
| macOS refuses to open the file | Right-click it and choose **Open** |
| `Permission denied` on Linux | Type `python3 irodori.py` |
| `Program Snapmaker Orca not found` | Install Orca, or download the 3MF and open it by hand. You can also set the path in the `IRODORI_ORCA` variable |
| `Program Blender (needed for FBX) not found` | Install Blender, or save the model as GLB |
| Port 8765 is taken | Set another one in the `IRODORI_PORT` variable |
| The model is white or has wrong colors | Add the missing textures when the program asks for them |

Your problem is not here? [Report it](https://github.com/Devoane/irodori/issues) and include your system, the file type and the error text.

## How it works

A toolchanger only holds a few spools, but the layers are very thin. When blue and yellow layers alternate, from a distance your eye sees green. A screen or a printed newspaper works in a similar way.

Irodori looks for the 2–4 spools that together cover the model's colors best and builds up to 251 mixes from them. Each part of the model gets the closest mix. It scores results with ΔE2000, a measure of color difference designed around human vision.

The export format, algorithms and tests are described in [docs/technical.md](docs/technical.md).

## Good to know before printing

- **Export is ready for the Snapmaker U1.** Projects are currently made for this printer and Snapmaker Orca 2.4, and they should not be sent to another printer without its own profile. Export works for 2–4 toolheads; the 5–8 toolhead mode is preview only.
- **The preview is a prediction.** The real color depends on the filament batch and the light. Card calibration is meant to improve this, but it has not yet been checked on a real print.
- **Waste can be high.** Mixes need a wipe tower. In one test the tower used 58 g and the model itself 13.5 g.
- **Very large models are simplified.** The default limit is 1 million triangles.
- **Tested on Linux.** The Windows and macOS launchers are included but have not been tried on those systems. If something fails, please report it.
- **Translations are unreviewed.** They were written with an AI assistant, and native speakers have not checked them. Filament names stay as the catalog gives them.

## Help translate

Spotted a bad translation, or want your language? Edit the file for your language in [`frontend/i18n`](frontend/i18n) and open a pull request. [docs/technical.md](docs/technical.md) explains how to add a language.

## Privacy

The program runs only on your computer and listens only on `127.0.0.1`. Models are not sent anywhere. Internet is only needed for the first download of the libraries.

## License

The code is available under the [GNU AGPL-3.0](LICENSE). Some of the color logic and data come from OrcaSlicer and Snapmaker Orca.

| Part | License |
|---|---|
| FilamentMixer mixing model (from Orca) | MIT |
| three.js, three-mesh-bvh | MIT |
| Antykwa Toruńska | GUST Font License |
| M PLUS 1, M PLUS 1p, Shippori Mincho | SIL OFL 1.1 |

Irodori is an independent project, not affiliated with any printer or filament maker. Their names are used only to describe compatibility.

## Contact

devoane911@protonmail.com
