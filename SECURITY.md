# 🔐 Security, Transparency & User Safety

**Vanilla True Builder (VTB)** is built on the principles of **100% transparency, zero bloat, and total user safety.** 

This document outlines our security architecture, verifying that the software is completely free of malware, spyware, and harmful background processes.

---

## 🔍 100% Open Source & Fully Auditable
* **Plain-Text Codebase:** The entire project is written in readable, uncompiled Python ([`main.py`](main.py) and [`auditor.py`](auditor.py)). Nothing is hidden or obfuscated.
* **Inspect Every Line:** Anyone can inspect, review, or modify the source code before running it. There are no encrypted payloads, packed loaders, or obfuscated scripts.
* **Run Directly via Interpreter:** You don't need to trust pre-compiled executables; you can audit the scripts and execute them directly via the official Python interpreter (`python main.py`).

---

## 🦠 Zero Malware, Viruses, or Telemetry
* **No Trojans / Keyloggers:** VTB does not contain backdoors, remote access trojans (RATs), crypto miners, or malicious payloads.
* **No Telemetry or Tracking:** VTB does not collect personal information, hardware IDs, Riot account credentials, or behavioral analytics.
* **Strictly Isolated Network Calls:** The software **only** connects to the internet for two public, read-only open-source resources:
  1. **CommunityDragon:** To download updated game hash dictionaries (`hashes.game.txt`).
  2. **GitHub Releases ([vtb-ltk-tools](https://github.com/gsmeven/vtb-ltk-tools)):** To fetch official, verified releases of `wadtools.exe` and `ritobin.exe`.

---

## ✔️ Verified Binary Supply Chain (`vtb-ltk-tools`)
VTB relies on two low-level binaries (`wadtools.exe` and `ritobin.exe`) to decompile and recompile League archive formats:
* These binaries are **not** bundled blindly from unknown third parties.
* They are built transparently using automated, verifiable GitHub Actions workflows hosted publicly on [**gsmeven/vtb-ltk-tools**](https://github.com/gsmeven/vtb-ltk-tools).
* Every release is reproducible directly from the official upstream C++/Rust codebases of [moonshadow565](https://github.com/moonshadow565) and [Crauzer](https://github.com/Crauzer).

---

## 🎮 Memory Safety & Game Client Integrity
* **No Memory Hooking:** VTB is strictly an **offline asset pre-compiler**. It never attaches to `League of Legends.exe` or `Riot Vanguard`.
* **No DLL Injection:** It does not inject code, hooks, or libraries into active game processes.
* **Standard Containers:** The engine strictly packages native asset files (`.bin`, `.skn`, `.tex`) into standard `.fantome` files, which are safely mounted via trusted community tools like [LTK Manager](https://github.com/LeagueToolkit).

---

## ⚠️ Antivirus False Positives
Because VTB interacts with game archives (`.wad.client`), executes command-line subprocesses, and downloads hash files over HTTPS, some generic heuristic antivirus engines may occasionally flag modding utilities as unknown software (*false positives*).

* **How to Verify:** Because this project is 100% open source, you can submit any script or binary from our releases to [VirusTotal](https://www.virustotal.com/) or inspect the Python source directly.
* If you encounter a false positive, we recommend whitelisting the project directory or running directly from source via `python main.py`.

---

## 📬 Contact & Inquiries
If you have questions regarding the safety, integrity, or architecture of Vanilla True Builder, feel free to open a public discussion or contact the maintainer:

* **Author:** [Neve](https://github.com/gsmeven)
* **Issues:** [GitHub Issues](https://github.com/gsmeven/vanilla-true-builder/issues)