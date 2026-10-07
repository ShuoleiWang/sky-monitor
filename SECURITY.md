# Security

Sky Monitor tells an observatory whether its roof may open. Please report privately anything that
could make it answer "safe" wrongly, reveal a camera address, password or mail code, or let another
computer press its buttons: on GitHub, open this repository's **Security** tab and choose
**Report a vulnerability**. Please do not describe such a problem in a public issue.

What the program protects, and what it does not:

- Every path that cannot measure the sky answers "not safe", and an answer that has run out is not
  safe either.
- Camera passwords, mail codes and keys stay in the settings file; logs, `status.json`, mails and
  error messages carry none of them.
- The status page is for the observatory computer unless `[web] share = "lan"` is set. Then other
  computers on the network may view it; its buttons need the control code (five wrong codes lock a
  computer out for ten minutes), and every button request must carry a header that a page from
  elsewhere cannot add. The page is plain HTTP: on the network the code is not encrypted.
- The ASCOM Alpaca devices answer the observatory computer only, unless `[alpaca] host` names a
  network address. Alpaca itself has no authentication.

Only the latest version receives fixes.
