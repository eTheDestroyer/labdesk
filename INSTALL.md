# Installing LabDesk: step by step

This guide takes you from nothing to a running LabDesk window. Do the steps in order.

* **On Windows 10 / 11?** Follow [LabDesk for Windows](#labdesk-for-windows) right below. No terminal needed.
* **On Linux?** Skip to [What you need first](#what-you-need-first). Commands go in a terminal (Konsole).
  Copy one block at a time.

---

## LabDesk for Windows

LabDesk also runs on Windows as a single program file, **LabDesk.exe**. Nothing to install.

### What you need

| You need | Why | Check it |
|---|---|---|
| **Windows 10 or 11** (64-bit) | | |
| **A running CML-Free VM that this PC can reach**, for example CML in VMware Workstation | LabDesk is a front end for CML. It does not include CML or any router images | open `https://<CML address>` in a browser and log in |
| **Your CML login** (usually `admin` and the password you set during CML setup) | LabDesk logs in to CML for you | |
| **Windows Terminal** (recommended; built into Windows 11, free in the Microsoft Store for Windows 10) | device consoles open as tabs in one "LabDesk" window | press Win+R, type `wt`, Enter |
| **OpenSSH client** (built into Windows 10/11) | consoles use it | press Win+R, type `cmd`, Enter, then type `ssh -V` |

> No Windows Terminal? Consoles still work: each one opens in its own console window instead of a tab.
> `ssh -V` says "not recognized"? Settings → Apps → Optional features → Add a feature → **OpenSSH Client**.

### Step 1: download LabDesk.exe

1. Open the repository on GitHub → **Releases** (right-hand side) → the newest release.
2. Under *Assets*, click **LabDesk.exe**. Save it somewhere you will find it again, for example your Desktop.

> Newest test build instead: **Actions** → *Build LabDesk.exe* → the latest green run → *Artifacts* →
> **LabDesk-windows** (a .zip with LabDesk.exe inside; right-click → *Extract All*).

### Step 2: run it

Double-click **LabDesk.exe**. The first start takes 5 to 10 seconds (it unpacks itself); later starts are faster.

**"Windows protected your PC"?** LabDesk is not code-signed, so SmartScreen warns about it. Click
**More info**, then **Run anyway**. You only do this once.

### Step 3: tell LabDesk where CML is

The first time, LabDesk asks for your CML login:

```
CML address:   192.168.80.130    ← the address you open in the browser to reach CML
Username:      admin
Password:      ••••••            ← the CML password
CML VM file:   (optional)        ← Browse… to the CML VM's .vmx file if CML runs in VMware Workstation
```

Click **Test connection**. When it says **✓ Connected**, click **Save**.

LabDesk saves this in `%APPDATA%\LabDesk\cml-credentials.env`, a file only your Windows account can open.
You can change it any time: toolbar **Lab** menu → **CML connection…**.

### Step 4: use it

Everything in [Step 5: first steps inside LabDesk](#step-5-first-steps-inside-labdesk) works the same, with two differences:

* **Double-click a device** to open its console: a tab in a Windows Terminal window called *LabDesk*.
  **The console asks for the CML password** (the same one as above) each time, unless you set up an SSH key
  (see below). Type it and press Enter; nothing shows while you type, that's normal. Tab, `?`, Ctrl-C and the
  arrow keys work as on a real router. Leave the console with **Ctrl-]**.
* **Start CML VM** starts CML in VMware Workstation when you gave the CML VM file in step 3.
  Otherwise, start the CML VM yourself (VMware, Hyper-V, …), wait about 2 minutes, and LabDesk reconnects by itself.

**No password in consoles (optional):** put an SSH key at `C:\Users\<you>\.ssh\labdesk_ed25519` and register its
`.pub` part for the CML `admin` user (README → *How consoles work*). LabDesk then uses it for consoles and the
background probes.

### What does not work on Windows

| Feature | On Windows |
|---|---|
| Juniper vJunos / Fortinet VMs (host KVM) | not available: they need Linux KVM. Greyed out in the device list |
| Internet cloud (always answers 8.8.8.8 / 1.1.1.1) | not available: it is a Linux network namespace. Labs that have one show it as *Linux only* |
| Start CML VM | only with VMware Workstation (`vmrun`) and the CML VM file set; otherwise start CML yourself |
| Telnet from the right-click *Management* menu | needs the Windows *Telnet Client* feature (Control Panel → Programs → Turn Windows features on or off) |

Everything else (designing labs, starting/stopping devices, consoles, live routing/switching map, hover cards,
templates, failure simulation) works the same as on Linux.

### If something goes wrong on Windows

| What you see | What to do |
|---|---|
| "Windows protected your PC" | **More info → Run anyway** (LabDesk is not signed) |
| **CML offline** in the status bar | Is the CML VM running? Can you open `https://<CML address>` in a browser on this PC? |
| *Test connection* says "No answer" | Wrong address, or CML is off, or the VM network is not reachable from Windows (in VMware, use NAT or Bridged) |
| *Test connection* says "wrong username or password" | Retype the CML password |
| A console window opens and closes at once | OpenSSH Client is missing: see *What you need* above |
| The hover card stays empty | The background reader logs in with the saved CML password (or the SSH key): check **Lab → CML connection…** |
| LabDesk closes right after starting | Send the file `%LOCALAPPDATA%\LabDesk\cache\labdesk.log` with your bug report |

To remove LabDesk from Windows, delete **LabDesk.exe**, and the folders `%APPDATA%\LabDesk` and `%LOCALAPPDATA%\LabDesk`
if you also want your saved login and settings gone.

---

## LabDesk for Linux

The rest of this guide is for Linux.

## What you need first

| You need | Why | Check it |
|---|---|---|
| **Linux with KDE** (tested on Debian 13) | LabDesk opens device consoles in Konsole | `konsole --version` |
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
4. **Double-click a device** to open its console in Konsole. Leave a console with **Ctrl-]**.
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
