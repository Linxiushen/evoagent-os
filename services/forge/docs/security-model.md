# Security Model

The scanner is defense in depth, not a sandbox. It catches common high-signal patterns but cannot prove the absence of malicious behavior, dependency attacks, obfuscation or runtime downloads.

Consumers should verify four independent facts: artifact digest, cryptographic signature, trusted key identity and acceptable capabilities. Installers should reject undeclared capabilities, apply least privilege, disable ambient credentials and isolate execution.

Private signing keys must stay outside repositories. Rotate compromised keys, publish a revocation statement, and never replace an existing version. Release a new version with a new trusted fingerprint instead.

