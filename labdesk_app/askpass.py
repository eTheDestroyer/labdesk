#!/usr/bin/python3 -I -S
"""SSH_ASKPASS helper: prints one password from the LabDesk env file.

ssh runs this when public-key auth is not accepted. The *name* of the variable comes from
LABDESK_ASKPASS_KEY (default CML_ADMIN_PASS); the value is only written to ssh's pipe.
Host-key confirmation prompts are answered "no" (never auto-accept via askpass).
"""
import os
import sys

CRED = os.path.expanduser(os.environ.get("CML_CRED_FILE", "~/cml-labs/cml-credentials.env"))
ALLOWED = {"CML_ADMIN_PASS", "JUNOS_ADMIN_PASS", "FGT_ADMIN_PASS"}


def main():
    prompt = " ".join(sys.argv[1:]).lower()
    if "yes/no" in prompt or "fingerprint" in prompt:
        print("no")
        return 0
    if "password" not in prompt and "passphrase" not in prompt:
        return 1
    key = os.environ.get("LABDESK_ASKPASS_KEY", "CML_ADMIN_PASS")
    if key not in ALLOWED:
        return 1
    try:
        with open(CRED, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(key + "="):
                    sys.stdout.write(line.split("=", 1)[1].strip().strip('"').strip("'") + "\n")
                    return 0
    except OSError:
        pass
    return 1


if __name__ == "__main__":
    sys.exit(main())
