# Contributing to Vanilla True Builder

Thank you for your interest in contributing to **Vanilla True Builder (VTB)**.

VTB is an automated visual restoration engine for League of Legends. Over **95% of champion skins revert automatically** via `main.py` without manual intervention. Contributions are strictly focused on curating the remaining edge cases (legendaries with unique state machines, decoupled deployable satellites, and submesh divergences) or refining the curation engine.

All skin curation in this project is executed through a specialized **AI-Assisted Workflow** combining the built-in Auditor studio (`auditor.py`), the forensic rulebook (`ai_context.md`), and any advanced reasoning AI (e.g., ChatGPT, Gemini, Claude, DeepSeek).

## Contribution Scopes

Contributions are accepted within the following scopes:

1. **Curated Overrides (`overrides/`):** Property templates for problematic skins and combat satellites generated via the AI-assisted Auditor pipeline.
2. **Curation Logic & Laws (`ai_context.md`):** Proposals to optimize or expand the *10 Laws of Curation*.

> [!NOTE]
> **Stability Baseline:** The 10 Laws of Curation have proven universally resilient in resolving model vanishing, weapon desynchronization, and animation locks with zero DirectX or client crashes. Any proposed changes to `ai_context.md` must preserve this 100% stability track record.

## Mandatory AI-Assisted Curation Workflow

Contributors must follow the native AI curation loop before opening a Pull Request:

```mermaid
flowchart TD
    A["1. Setup AI\nLoad ai_context.md into Reasoning AI"] --> B["2. Audit\nSelect skin & click AUDIT in auditor.py"]
    B --> C["3. AI Triage\nClick COPY REPORT & paste into AI chat"]
    C --> D{"AI Verdict:\nFix Required?"}
    D -- "No (Linear / Safe)" --> E["Discard\nHandled automatically by main.py in 0ms"]
    D -- "Yes (Broken Kit)" --> F["4. Copy Payload\nRight-click requested row › Copy payload › Send to AI"]
    F --> G["5. AI Generation\nAI outputs template complying with 10 Laws"]
    G --> H["6. Edit & Save\nDouble-click row in Auditor to paste template into overrides/"]
    H --> I["7. Isolated Test\nClick TEST in auditor.py › Verify in-game via LTK"]
    I --> J["8. Pull Request\nSubmit verified override file(s)"]
```

### Step 1: Initialize the AI
Open any reasoning AI chat and supply the contents of `ai_context.md` (located in the project root). This loads the forensic instructions, engine constraints, and the 10 Laws of Curation into the AI model.

### Step 2: Extract Telemetry in the Auditor
Launch `auditor.py` (or click **`SKIN FIX`** in the main builder). Select the target champion and skin, then click **`AUDIT`**. The Auditor pulls and decompiles only the relevant binary structures (`PROP`) and 3D submeshes (`.skn`) into RAM.

### Step 3: Forensic AI Triage
Click **`📋 COPY REPORT`** at the bottom of the Auditor and paste the diagnostic ledger into your AI chat. The AI will evaluate whether the divergences cause actual gameplay/visual breakage or if they are linear forms already handled automatically by `main.py` in 0ms.

### Step 4: Extract Specific Payload
If the AI determines that an override is strictly required, it will identify the exact row(s) (main champion body or satellite deployable). In the Auditor, right-click **only the requested row** and select **`Copy payload`**, then paste it to the AI.

### Step 5: AI Code Generation & Save
The AI will generate the property override adhering strictly to the **10 Laws of Curation**. Double-click that same row in the Auditor to immediately open its corresponding file in `overrides/`, paste the AI-generated template, and save.

### Step 6: Isolated In-Game Test
Click the **`TEST`** button in the Auditor. This compiles a standalone, isolated `.fantome` test mod for that specific champion and skin. Mount it in [LTK Manager](https://github.com/LeagueToolkit) and enter a practice game to verify that models, animations, hitboxes, and spell effects render canonically without crashes or invisible submeshes.

### Step 7: Submit Pull Request
Once verified in-game, commit strictly the final `.bin.txt` override file(s) and submit your Pull Request.

## Technical Quality Gates

Pull Requests failing any of these technical gates will not be merged:

* **Indentation:** Strictly **TABS (`\t`)**. Spaces are rejected by the Ritobin parser.
* **Base Parity (Law 7):** The `resourceMap` must be a 1:1 clone of `Skin0`. All target-skin exclusive keys, variant emotes, jokes, and dead branches (`0x00000000`) must be purged.
* **Zero External Binaries (Law 9):** Never commit binary files (`.skn`, `.dds`, `.wad`, `.exe`). 3D submesh discrepancies must be solved exclusively via Line 2 `# SUBMESH_ALIAS:` directives (patched in memory by the engine in 0ms).
* **Variant Declarations:** If parent skins cover chromas, declare them on Line 1 using `# VARIANTS: <IDs>` (e.g., `# VARIANTS: 20-27, 56`). Never generate duplicate templates for linear variants.
* **RAM Bridge (Law 1):** Deployable satellites must link both the satellite `.bin` and parent champion `.bin` so shared particle pointers resolve correctly.
* **No File Comments:** No `#` comments within the file body after the initial header directives (`# VARIANTS:` and `# SUBMESH_ALIAS:`).

## Pull Request Guidelines

1. **Atomic Commits:** Scope each PR to a single champion family (main champion override and associated combat satellites).
2. **Canonical Paths:** Place files strictly in the standard structure:
   ```text
   overrides/<entity>/skins/skin<id>.bin.txt
   ```
3. **Verification Note:** State the target champion, skin ID, and brief confirmation that the fix was tested via the Auditor `TEST` button in-game.

## License

By contributing to Vanilla True Builder, you agree that your contributions will be licensed under the project's [GNU General Public License v3.0](LICENSE).