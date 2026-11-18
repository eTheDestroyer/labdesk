import sys, re, time, pexpect, json
sys.path.insert(0, '/home/lf/cml-labs/labdesk')
from labdesk_app import launchers
KEYS = {"R1":"ad62403d-e0e6-4c80-bf90-5dd999ac9a9f","R2":"225663e3-de05-4d57-9f80-b6c5228c9b68",
        "R3":"539f48a0-8da7-4be1-bc02-6883ed1d22d6","R4":"d5711300-1654-4b4c-baeb-e0f7ac73d6c9"}
PROMPT = re.compile(rb"\n(R\d)(\([\w\-]+\))?([>#]) ?$")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
class Con:
    def __init__(self, name):
        argv = launchers.cml_ssh_base("admin") + ["connect", KEYS[name]]
        argv = [a for a in argv]  # -t present
        t=time.time()
        self.p = pexpect.spawn(argv[0], argv[1:], timeout=30, maxread=200000, env={"PATH":"/usr/bin:/bin","TERM":"vt100"})
        self.p.expect([rb"Escape character is.*\n", pexpect.TIMEOUT], timeout=15)
        self.connect_time=time.time()-t
        self.sync()
    def sync(self):
        for _ in range(6):
            self.p.send("\r")
            i = self.p.expect([rb"[>#] ?$", rb"\[yes/no\]", rb"RETURN to get started", rb"--More--", pexpect.TIMEOUT], timeout=5)
            if i==0:
                time.sleep(0.3)
                try: self.p.read_nonblocking(100000, timeout=0.3)
                except Exception: pass
                break
            if i==1: self.p.sendline("no")
            if i==3: self.p.send("q")
        self.p.send("end\r"); self.p.expect(rb"#|>")
        self.p.sendline("enable"); 
        i=self.p.expect([rb"Password:", rb"#\s*$", rb">\s*$"], timeout=5)
        if i==0: self.p.sendline("cisco"); self.p.expect(rb"#\s*$")
        self.p.sendline("terminal length 0"); self.p.expect(rb"#\s*$")
        self.p.sendline("terminal width 0"); self.p.expect(rb"#\s*$")
        time.sleep(0.3)
        try: self.p.read_nonblocking(100000, timeout=0.3)
        except Exception: pass
    def run(self, cmd, timeout=60):
        t=time.time()
        self.p.sendline(cmd)
        self.p.expect(re.escape(cmd.encode()[-20:]) , timeout=timeout)
        self.p.expect(rb"\r?\n(R\d)#\s*$", timeout=timeout)
        out = self.p.before.decode("utf-8","replace")
        out = ANSI.sub("", out).replace("\r\n","\n").replace("\r","")
        return out.lstrip("\n"), time.time()-t
    def cfg(self, lines):
        self.p.sendline("configure terminal"); self.p.expect(rb"\(config\)#")
        for l in lines:
            self.p.sendline(l); self.p.expect(rb"\)#\s*$")
        self.p.sendline("end"); self.p.expect(rb"#\s*$")
    def close(self):
        self.p.sendcontrol("]"); self.p.close(force=True)
if __name__=="__main__":
    c=Con(sys.argv[1]); print("connect",round(c.connect_time,2))
    for cmd in sys.argv[2:]:
        o,t=c.run(cmd); print(f"### {cmd} ({t:.2f}s)\n{o}")
    c.close()
