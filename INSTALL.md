# Installing LabDesk: step by step

This guide takes you from nothing to a running LabDesk window. Do the steps in order.

* **Just want the git commands?** [DOWNLOAD.md](DOWNLOAD.md) is the short version: download with `git`, install and
  open, for Linux and for Windows.
* **On Windows 10 / 11?** Follow [LabDesk for Windows](#labdesk-for-windows) right below. No terminal needed.
* **On Linux?** Skip to [What you need first](#what-you-need-first). Commands go in a terminal (Konsole).
  Copy one block at a time.

---

## LabDesk for Windows

On Windows, LabDesk runs **everything on the PC itself**: CML (with its router images), the Juniper vJunos and
Fortinet VMs and the Internet cloud run in **VMware Workstation**, set up and started by LabDesk. You start
LabDesk; it does the rest (click and play).

KVM exists only on Linux, so the VMs come as a ready-made folder, **LabDesk-VMs**, that you build once on the
Linux PC and copy to Windows. It is about 16 GB and holds licensed images and your CML password, so it is not in
git (`.gitignore` excludes it). Copy it only to your own PCs.

### What you need

| You need | Why |
|---|---|
| **Windows 10 or 11** (64-bit), **16 GB RAM** recommended (CML gets 8 GB) | |
| **VMware Workstation Pro** (free for personal use) | runs the VMs. LabDesk installs it silently when its installer is in `LabDesk-VMs\installer\` |
| **The LabDesk-VMs folder** (step 1) | CML + router images, vJunos, FortiOS, Internet cloud, your CML login |
| **Virtualization on, Hyper-V off** | CML runs routers inside its VM (nested virtualization). VMware cannot offer that while Windows' Hyper-V is on: turn off *Windows Security → Device security → Core isolation → Memory integrity* and run `bcdedit /set hypervisorlaunchtype off` as administrator, then restart |

### Step 1: build the LabDesk-VMs folder (on the Linux PC, once)

```bash
cd ~/labdesk                         # (or ~/cml-labs/labdesk)
.venv/bin/python tools/make_windows_bundle.py
```

It prepares the CML VM for VMware (harmless under KVM), shuts CML down for about 5 minutes to copy its disk,
starts it again (start your running labs again afterwards), converts vJunos and FortiOS from `~/Downloads`, and
writes `~/LabDesk-VMs`. Put the VMware installer (`VMware-workstation-full-*.exe`, from Broadcom's download
portal) into `~/Downloads` first if you want LabDesk to install VMware for you.

### Step 2: copy it next to LabDesk on Windows

Copy the whole `LabDesk-VMs` folder (USB disk, your own cloud drive, network share) to **one** of:

* inside the git folder: `C:\Users\<you>\labdesk\LabDesk-VMs` (git ignores it), or
* next to `LabDesk.exe`, or `C:\Users\<you>\LabDesk-VMs`.

Use a local disk (SSD), not a USB stick: the VMs run from this folder.

### Step 3: start LabDesk

Double-click **`labdesk.bat`** (git) or **`LabDesk.exe`**. The first time LabDesk:

1. takes the CML login from the folder - **no connection dialog**;
2. installs VMware Workstation if it is missing and the installer is in the folder;
3. creates the LabDesk network (VMnet, 192.168.122.0/24, CML at 192.168.122.60) - **Windows asks once for
   administrator rights**;
4. starts CML in VMware and waits for it (2 to 4 minutes; the event log shows each step).

Later starts: LabDesk just starts CML when it is off. The status bar shows **CML 2.x · 192.168.122.60** when ready.

> **"Windows protected your PC"** (LabDesk.exe): LabDesk is not code-signed. Click **More info → Run anyway**.

### What works on Windows

Everything that works on Linux:

| Feature | On Windows |
|---|---|
| Labs, templates, live map, hover cards, failure simulation | same as Linux |
| Device consoles | tabs of the **LabDesk console window** (neon look, scrollback, lens). CML consoles log in by themselves - no password to type |
| Juniper vJunos / Fortinet FortiOS | VMware VMs (linked clones of the folder's templates). Console = the VM's serial port, in the LabDesk console window; **Ctrl-]** leaves it. A new link to a running VM is added at its next start |
| Internet cloud (8.8.8.8 / 1.1.1.1) | runs inside the CML VM; health and return routes work as on Linux |
| Start CML VM | automatic at start-up (switch off: setting `cml/autostart=false`) |
| Telnet / SSH from the right-click *Management* menu | opens in Windows Terminal (Telnet needs the Windows *Telnet Client* feature) |

> vJunos-router and FortiOS are supported by Juniper / Fortinet on KVM only; on VMware they are best effort.

### If something goes wrong on Windows

| What you see | What to do |
|---|---|
| "VMware Workstation is not installed" | Install VMware Workstation Pro, or put its installer in `LabDesk-VMs\installer\`, then start LabDesk again |
| "…nested virtualization… Hyper-V…" | Turn off Memory integrity and Hyper-V (see *What you need*), restart Windows |
| "the administrator prompt was cancelled" | Start LabDesk again and click **Yes** in the Windows prompt (needed once, for the VMware network) |
| **CML offline** for more than 5 minutes | Open VMware Workstation → *LabDesk CML* to see its screen. LabDesk also follows CML if VMware's DHCP gave it another address |
| "no LabDesk-VMs folder found" | Copy the folder to one of the places in step 2 |
| "the VM is open in the VMware Workstation window" | Close that VM's tab in VMware Workstation (LabDesk drives the VMs itself) |
| LabDesk closes right after starting | Send `%LOCALAPPDATA%\LabDesk\cache\labdesk.log` with your bug report |

**Without the folder** LabDesk still works as a plain CML front end: the first start asks for the CML address and
login (a CML that Windows can reach), and *Start CML VM* uses a VMware `.vmx` you choose under
**Lab → CML connection…**.

To remove LabDesk from Windows: delete LabDesk (and `LabDesk-VMs`), plus `%APPDATA%\LabDesk` and
`%LOCALAPPDATA%\LabDesk` for settings and login.

---

## LabDesk for Linux

The rest of this guide is for Linux.

## What you need first

| You need | Why | Check it |
|---|---|---|
| **Linux with KDE** (tested on Debian 13) | LabDesk opens device consoles in its own console window; Konsole is the fallback | `konsole --version` |
| **KVM / libvirt** | CML runs as a virtual machine | `virsh list --all` |
| **Cisco CML-Free VM**, installed and working | LabDesk is a front end for CML. It does not include CML or any router images | open `https://<CML address>` in a browser and log in |
| **Your CML login** (usually `admin` and the password you set during CML setup) | LabDesk logs in to CML for you | |

CML-Free is a free download from Cisco (developer.cisco.com/modeling-labs). It ships the IOSv router and IOSvL2 switch images.
Install it as a KVM VM and finish its first-boot setup before you continue here.

---

## Step 1: install the system packages

```bash
sudo apt update
sudo apt install git python3 python3-venv konsole openssh-client libvirt-clients libxcb-cursor0
```

Your user also needs to be in the `libvirt` group, so LabDesk can start and stop the CML VM:

```bash
sudo usermod -aG libvirt $USER
```

Log out and back in once after this command.

## Step 2: download LabDesk

The repo is **private**, so first log in to GitHub on this PC (one time only):

```bash
gh auth login          # pick GitHub.com → HTTPS → "Login with a web browser"
```

> No `gh`? Install it with `sudo apt install gh`, or copy a personal access token from
> GitHub → Settings → Developer settings and use it as the password when `git clone` asks.

Then download the code into your home folder:

```bash
cd ~
git clone https://github.com/eTheDestroyer/labdesk.git
cd labdesk
```

You now have a folder `~/labdesk` with the program in it.

## Step 3: run the installer

From inside that folder:

```bash
./install.sh
```

The installer does five things and prints a ✓ for each one:

1. **Checks** that the packages from step 1 are there. If one is missing, it prints the exact `sudo apt install …` line to run.
2. **Installs** LabDesk's Python packages (PySide6 and others) into a private folder `~/labdesk/.venv`.
   The first time takes a minute or two. Nothing is installed system-wide.
3. **Asks for your CML login**, once:
   ```
   CML address [192.168.122.60]:    ← press Enter if that's your CML's address
   CML username [admin]:            ← press Enter for admin
   CML password:                    ← type it; nothing shows while you type, that's normal
   ```
   It saves them to `~/cml-labs/cml-credentials.env`. Only you can read that file, and it is **not** part of the Git folder,
   so it is never uploaded.
4. **Adds LabDesk to the application menu.**
5. **Checks that CML answers.** If CML is switched off you'll see a warning. That's fine: LabDesk can start it for you.

## Step 4: open the program

Either:

* open the **application menu** and type **LabDesk** (it is under *Internet / Network*), or
* run it from the terminal:
  ```bash
  ~/labdesk/labdesk
  ```

To pin it to the taskbar, right-click the LabDesk icon in the menu and choose *Pin to Task Manager*.

## Step 5: first steps inside LabDesk

1. If the status bar says **CML offline**, click **Start CML VM** and wait about 2 minutes.
2. Click **From template…** and pick a ready-made lab, for example:
   * `3-router-hsrp.yaml`: 3 routers with HSRP,
   * `hub-spoke-hsrp-bgp.yaml`: hub and spoke with HSRP, BGP and access lists,
   * `switching-lab-etherchannel.yaml`: switches with VLANs, LACP and PAgP.
3. Click **Start all**. Routers take 2 to 5 minutes to boot. A green ✓ on a device means it is up.
4. **Double-click a device** to open its console: a tab in the green **LabDesk console** window (one tab per
   device). Leave a console with **Ctrl-]**.
5. **Hover over a device** to see its live information, including its **first login** (what to type the first time
   you open its console - LabDesk saves it for you, nothing to set up). Cisco routers and switches: press Enter, then
   `enable` → `cisco` (SSH / telnet: `admin` / `cisco`); Linux PCs: `cisco` / `cisco`. Right-click a device →
   **Copy password** / **Credentials…** to copy or change it. All platforms: README, "First logins".

6. **Choose what each device shows when its console opens:** right-click a router or switch → **Show script…**,
   type the `show` commands you want for that device (or click **Suggest from live data**), and **Save**.
   README, "Show script per device".

CML-Free runs at most **5 devices at a time**. The counter in the toolbar (`CML n/5`) shows how many are running.

---

## Updating to a newer version

```bash
cd ~/labdesk
git pull
./install.sh          # quick: it keeps your saved login
```

## Changing the CML address or password

```bash
cd ~/labdesk
./install.sh --reconfigure
```

Or, inside LabDesk: toolbar **Lab** menu → **CML connection…**.

## Optional extras

These are not needed to use LabDesk. The details are in [README.md](README.md).

* **Consoles without a password prompt:** set up SSH key login to the CML console server (README → *How consoles work*).
  Without it, LabDesk types the password for you, so consoles still work.
* **Internet cloud** that always answers ping to 8.8.8.8 and 1.1.1.1:
  `sudo ~/labdesk/host/labdesk-cloud/install.sh` (README → *Internet cloud*).
* **Juniper vJunos / Fortinet VMs:** put the vendor images in `~/Downloads` (README → *Host-KVM devices*).

---

## If something goes wrong

| What you see | What to do |
|---|---|
| `./install.sh: Permission denied` | `chmod +x install.sh labdesk` and run it again |
| The installer says *Missing packages* | Run the `sudo apt install …` line it prints, then `./install.sh` again |
| `could not load the Qt platform plugin "xcb"` | `sudo apt install libxcb-cursor0` |
| LabDesk isn't in the menu | Log out and back in, or start it with `~/labdesk/labdesk` |
| **CML offline** in the status bar | Click **Start CML VM**, or run `virsh start cml` and wait 2 minutes |
| *CML authentication failed* | The saved password is wrong: `./install.sh --reconfigure` |
| *Start CML VM* fails with "permission denied" | Your user isn't in the `libvirt` group: see step 1, then log out and in |
| A console tab asks for a password | That's the CML password (or set up SSH keys, see *Optional extras*) |
| The hover card says "needs enable secret" | Right-click the device → **Credentials…** and enter the enable password (`cisco` in the templates) |
| A device won't start: "limit" | CML-Free runs only 5 devices. Stop one first |

## Removing LabDesk

```bash
cd ~/labdesk
./install.sh --uninstall      # removes the menu entry and the .venv folder
cd ~ && rm -rf ~/labdesk      # removes the program itself
```

Your saved CML login (`~/cml-labs/cml-credentials.env`) and settings (`~/.config/labdesk`, `~/.config/LabDesk`) are kept.
Delete them by hand if you want them gone too.
