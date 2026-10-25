# Security, Transparency & User Safety

Vanilla True Builder (VTB) is engineered on the principles of full transparency, minimal attack surface, and user safety.

This document outlines our security architecture, confirming that the software is free of malicious payloads, spyware, and unauthorized background processes.

## 100% Open Source & Auditable

* Plain-Text Codebase: The entire project is written in readable, uncompiled Python (`main.py` and `auditor.py`). No code is obfuscated or encrypted.
* Independent Audit: Users and security researchers can inspect, review, or modify the source code directly. There are no packed loaders or hidden payloads.
* Direct Execution: You can execute the software directly via the official Python interpreter (`python main.py`) without relying on pre-compiled executables.

## Zero Malware, Telemetry, or Credential Access

* No Backdoors or Miners: VTB contains no remote access trojans (RATs), crypto miners, keyloggers, or background persistence services.
* No Telemetry: VTB does not collect personal data, Riot account credentials, hardware IDs, IP logs, or usage analytics.
* Minimal Network Surface: The application connects to external networks solely for two public, read-only resources:
  1. CommunityDragon: To synchronize game hash dictionaries (`hashes.game.txt`).
  2. GitHub Releases (`vtb-ltk-tools`): To fetch verified release archives containing `wadtools.exe` and `ritobin.exe`.

## Verified Binary Supply Chain (vtb-ltk-tools)

VTB relies on two low-level utilities (`wadtools.exe` and `ritobin.exe`) to decompile and recompile League archive formats:
* Upstream Verification: Binaries are never bundled blindly from untrusted third parties.
* Reproducible Builds: Binaries are compiled via automated, publicly verifiable GitHub Actions workflows hosted on [gsmeven/vtb-ltk-tools](https://github.com/gsmeven/vtb-ltk-tools).
* Upstream Codebases: Every build originates directly from the official open-source repositories of moonshadow565 and Crauzer.

## Memory Safety & Game Client Integrity

* No Process Attachment: VTB is strictly an offline asset pre-compiler. It never attaches to or reads memory from `League of Legends.exe` or `Riot Vanguard`.
* No DLL Injection: The software does not inject code, hooks, or dynamic libraries into active game processes.
* Standard Archives: Output files are packaged into standard `.fantome` archives, mounted externally through trusted community managers such as [LTK Manager](https://github.com/LeagueToolkit).

## Antivirus False Positives

Because VTB modifies game archive structures (`.wad.client`), spawns command-line subprocesses, and downloads hash files over HTTPS, generic heuristic engines may occasionally flag modding utilities as unknown software.

* Verification: Source code and binaries can be independently verified on [VirusTotal](https://www.virustotal.com/) or run directly from source via `python main.py`.
* Resolution: If your security suite flags the executable, whitelist the project directory or run directly through the Python interpreter.

## Contact & Inquiries

For security inquiries or vulnerability reports, please open an issue on our tracker:

* Author: [Neve](https://github.com/gsmeven)
* Issues: [GitHub Issues](https://github.com/gsmeven/vanilla-true-builder/issues)