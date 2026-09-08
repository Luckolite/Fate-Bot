# Security and public source

The public distribution omits authentication files, private data lists, the
excluded games and their history. Its generated configuration has blank
credentials, no configured bot owners, localhost-only listeners, development
mode off, automatic bot start off, and host reboot off.

Configure your own Discord application, owner IDs, database credentials and
OAuth redirect before running Fate. Keep auth files, keys and environment
files outside version control. Use HTTPS for remotely accessible services;
keep the control panel on a private network or behind an authenticated proxy.
Never expose dashboard development login to the internet.

Publication runs a secret scan and rejects new findings. A scan is not proof
that the application is vulnerability-free. Review dependency advisories and
deployment configuration before exposing a running instance. Public source
code and a publicly reachable deployment are separate security boundaries.

Report suspected vulnerabilities privately to the repository owner. Do not
include credentials or private user data in public issues.
