# Contributing to Vanilla True Builder

Thank you for your interest in contributing to **Vanilla True Builder (VTB)**.

Over 95% of champion skins revert automatically via `main.py` without manual intervention. Community contributions are strictly focused on curating the remaining rare edge cases.

The entire curation process in this project is executed through the built-in Auditor (`auditor.py`) workflow (**`HELP`** guide), combining the curation rulebook (`ai_context.md`) with AI assistance.

## Contribution Scopes

Contributions are accepted strictly within the following scopes:

1. **Curated Overrides (`overrides/`):** Plain-text property templates (`.bin.txt`) for problematic skins and combat satellites.
2. **Curation Rules (`ai_context.md`):** Proposals to optimize or expand the 10 Laws of Curation.

> [!NOTE]
> The 10 Laws of Curation have proven universally resilient in restoring broken skins with zero crashes. Any proposed changes to `ai_context.md` must be strictly non-redundant, proven necessary to achieve 100% game coverage, and represent a decisive rule that enabled the AI to fix a concrete broken skin.

## Mandatory Curation Workflow

Contributors must follow the native curation loop before opening a Pull Request:

```mermaid
flowchart TD
    A["<b>1. Setup AI</b><br/>Load <i>ai_context.md</i> into Reasoning AI"] --> B["<b>2. Audit</b><br/>Select skin & click [ AUDIT ] in <i>auditor.py</i>"]
    B --> C["<b>3. Triage Report</b><br/>Paste [ COPY REPORT ] to identify target entity"]
    C --> D["<b>4. Copy Payload</b><br/>Right-click target row › [ Copy payload ]"]
    D --> E["<b>5. Generate Template</b><br/>AI produces .bin.txt adhering to 10 Laws"]
    E --> F["<b>6. Save Override</b><br/>Double-click row in Auditor to paste & save"]
    F --> G["<b>7. Isolated Test</b><br/>Click [ TEST ] › In-game verification via LTK"]
    G --> H(["<b>8. Pull Request</b><br/>Submit canonical .bin.txt override"])
```

### Step 1: Setup AI Chat
Provide the contents of `ai_context.md` (located in the project root) to your reasoning AI chat. *(Shortcut: You can also click **`HELP`** › **`Copy Context`** directly inside the Auditor).*

### Step 2: Run Diagnostics in the Auditor
Launch `auditor.py` (or click **`SKIN FIX`** in the main builder). Select the target champion and skin, then click **`AUDIT`**. The Auditor decompiles and extracts strictly the required properties (`PROP`) and 3D submeshes (`.skn`) into RAM.

### Step 3: Triage Diagnostics
Click **`📋 COPY REPORT`** at the bottom of the Auditor and paste the diagnostics into your AI chat to identify the exact divergent entity (main champion body, deployable combat satellite, or submesh contract mismatch).

### Step 4: Extract Target Payload
Once the target entity is confirmed, right-click its specific row in the Auditor, select **`Copy payload`**, and send it to the AI.

### Step 5: Generate Property Template
The AI generates the property override strictly adhering to the 10 Laws of Curation, establishing 1:1 Base parity and declaring `# VARIANTS:` or `# SUBMESH_ALIAS:` headers if required.

### Step 6: Save Override to Disk
Double-click that same entity row in the Auditor to open its corresponding file in `overrides/`. Paste the AI-generated template and save (`Ctrl + S`).

### Step 7: Isolated In-Game Test
Click **`TEST`** inside the Auditor to compile an isolated `.fantome` package. Mount it in [LTK Manager](https://github.com/LeagueToolkit) and enter Practice Tool to verify that models, submeshes, hitboxes, and spell effects render canonically without crashes or visual artifacts.

### Step 8: Submit Pull Request
Once verified in-game, commit the generated `.bin.txt` file(s) strictly under `overrides/<entity>/skins/skin<id>.bin.txt` (preserving the exact entity structure created by the Auditor) and open your Pull Request.

---

## Pull Request Guidelines

1. **Focused PRs:** Scope each pull request to a single champion family (the main champion override and its combat satellites).
2. **Canonical Paths:** Place files strictly in the standard directory hierarchy:
   ```text
   overrides/<entity>/skins/skin<id>.bin.txt
   ```
3. **Verification Proof:** Mention the target champion, skin ID, and brief confirmation that the fix was verified in-game via the Auditor **`TEST`** button.
4. **Law Compliance:** Submitted templates must strictly adhere to the 10 Laws in `ai_context.md` (plain-text `.bin.txt` only, no binary files).

---

## License

By contributing to Vanilla True Builder, you agree that your contributions will be licensed under the project's [GNU General Public License v3.0](LICENSE).
