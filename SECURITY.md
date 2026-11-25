# Security policy

Security reports should identify the affected release or checkout. For an installed package, obtain the version with `python -m pip show chronodict`. For a source checkout, include the commit ID.

## Report a vulnerability privately

Open the repository's [Security advisories](https://github.com/ChronoDict/ChronoDict/security/advisories) page and use **Report a vulnerability** when the private form is available. If the form is unavailable, open a [new issue](https://github.com/ChronoDict/ChronoDict/issues/new) asking for a private reporting channel. Keep that issue limited to the contact request. [GitHub private reporting guide](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/report-privately)

Use the private channel to provide:

- The affected version or commit, Python version, operating system, and selected backend.
- The observed behavior, impact, and conditions under which it occurs.
- A minimal reproduction using synthetic data, with credentials, personal data, and production records removed.

Keep technical details private while coordinating disclosure with the maintainers. The maintainers can use a repository advisory to communicate affected versions and fixes.

ChronoDict runs in the calling application's process. Applications control access to stored data and the callbacks they supply. Ordinary bugs and usage questions can go through the public [issue tracker](https://github.com/ChronoDict/ChronoDict/issues).
