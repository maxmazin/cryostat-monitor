# Windows-host scripts — WSL boot task

`cryostat-wsl-boot-task.xml` is the committed Task Scheduler definition for the
task that boots the WSL2 distro at Windows startup and keeps it alive
(the manual steps it replaces are in [`docs/deployment-wsl.md`](../../docs/deployment-wsl.md) §5).
Without it the server stack simply does not come back after a reboot.

## Import

Edit the `<UserId>` placeholder in the XML (the Windows account that installed
Ubuntu), or override it with `/ru`. In an **elevated** PowerShell/cmd:

```
schtasks /create /tn "cryostat-wsl-boot" /xml cryostat-wsl-boot-task.xml /ru <DOMAIN\user>
```

It prompts for that account's password — the task runs "whether user is logged
on or not," which stores the credentials.

## Verify

```
schtasks /query /tn cryostat-wsl-boot /v
schtasks /run   /tn cryostat-wsl-boot        # start it now without rebooting
```

Then the real test: **reboot Windows**, wait a minute, and confirm
`Invoke-RestMethod http://localhost:8000/health` answers with nobody logged in
(deployment-wsl.md §5 "Reboot test").

## Known failure modes

- **Windows password change → result code `0x8007052E`.** The stored credentials
  are invalidated and the task silently stops working; the stack is then down
  after the next reboot. Fix: re-save the task (re-enter the password in Task
  Scheduler, or re-run the `schtasks /create ... /ru` import). Check "Last Run
  Result" in `schtasks /query /v` after **any** password change.
- **Windows feature updates** can drop or disable scheduled tasks. Re-verify
  (`schtasks /query /tn cryostat-wsl-boot /v` + a reboot test) after each one.
