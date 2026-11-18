# Installing LabDesk: step by step

This guide takes you from nothing to a running LabDesk window. Do the steps in order.
Commands go in a terminal (Konsole). Copy one block at a time.

---

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
5. **Hover over a device** to see its live information. To save its login, right-click it and choose **Credentials…**.
   The lab templates use `admin` / `cisco`.

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
