#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable
import tkinter as tk
from tkinter import messagebox, ttk

if getattr(sys, "frozen", False):
	WORKSPACE_ROOT = Path(sys.executable).resolve().parent
else:
	WORKSPACE_ROOT = Path(__file__).resolve().parent

APP_VERSION = "v1.8.3"	

OVERRIDES_DIR = WORKSPACE_ROOT / "overrides"
REPORT_FILE = WORKSPACE_ROOT / "auditor_ledger.log"
AI_CONTEXT_FILE = WORKSPACE_ROOT / "ai_context.md"

TOOLS_DIR = WORKSPACE_ROOT / "tools"
WADTOOLS = TOOLS_DIR / "wadtools.exe" if (TOOLS_DIR / "wadtools.exe").is_file() else WORKSPACE_ROOT / "wadtools.exe"
RITOBIN = TOOLS_DIR / "ritobin.exe" if (TOOLS_DIR / "ritobin.exe").is_file() else WORKSPACE_ROOT / "ritobin.exe"
HASHES_DIR = TOOLS_DIR / "hashes"
HASHES_GAME = HASHES_DIR / "hashes.game.txt"
SKINS_METADATA_FILE = HASHES_DIR / "skins.json"
SKINS_VERSION_FILE = SKINS_METADATA_FILE.with_suffix(".version")

MAX_PARALLEL_JOBS = max(1, int((os.cpu_count() or 4) * 0.60))
REPORT_LOCK = threading.Lock()


def get_ai_context() -> str:
	search_paths = []
	if hasattr(sys, "_MEIPASS"):
		search_paths.append(Path(getattr(sys, "_MEIPASS")) / "ai_context.md")
	search_paths.append(AI_CONTEXT_FILE)

	for p in search_paths:
		if p.is_file():
			try:
				return p.read_text(encoding="utf-8")
			except Exception:
				pass
	return "# AI CONTEXT ERROR: ai_context.md not found in workspace root."


def is_valid_champion_wad(wad_path: Path) -> bool:
	name = wad_path.name.lower()
	if not name.endswith(".wad.client"):
		return False
	if any(tag in name for tag in ("tft", "jade_")):
		return False
	if re.search(r"\.[a-z]{2}[_-][a-z]{2}\.", name):
		return False
	return bool(re.match(r"^[a-z0-9]+\.wad\.client$", name))


def detect_league_champions_dir() -> Path | None:
	default_path = Path(r"C:\Riot Games\League of Legends\Game\DATA\FINAL\Champions")
	if default_path.is_dir():
		return default_path
	import string
	for letter in string.ascii_uppercase:
		candidate = Path(f"{letter}:/Riot Games/League of Legends/Game/DATA/FINAL/Champions")
		if candidate.is_dir():
			return candidate
	return None


CHAMPIONS_DIR = detect_league_champions_dir()


def resolve_app_icon() -> Path | None:
	search_dirs: list[Path] = []
	if hasattr(sys, "_MEIPASS"):
		search_dirs.append(Path(getattr(sys, "_MEIPASS")))
	search_dirs.extend([WORKSPACE_ROOT, TOOLS_DIR])

	priority_names = ("rakuzan.ico", "icon.ico")
	for base_dir in search_dirs:
		for name in priority_names:
			candidate = base_dir / name
			if candidate.is_file():
				return candidate
		found_icos = list(base_dir.glob("*.ico"))
		if found_icos:
			return found_icos[0]
	return None


# ==============================================================================
# HASH SYNCHRONIZATION & AUTO-UPDATE INTEGRATION (COMMUNITYDRAGON & VTB-LTK-TOOLS)
# ==============================================================================
_COMMUNITY_DRAGON_RITOBIN_URLS = {
	"hashes.binentries.txt": "https://raw.githubusercontent.com/CommunityDragon/Data/master/hashes/lol/hashes.binentries.txt",
	"hashes.binfields.txt": "https://raw.githubusercontent.com/CommunityDragon/Data/master/hashes/lol/hashes.binfields.txt",
	"hashes.binhashes.txt": "https://raw.githubusercontent.com/CommunityDragon/Data/master/hashes/lol/hashes.binhashes.txt",
	"hashes.bintypes.txt": "https://raw.githubusercontent.com/CommunityDragon/Data/master/hashes/lol/hashes.bintypes.txt",
}

LTK_TOOLS_ZIP_URL = "https://github.com/gsmeven/vtb-ltk-tools/releases/download/tools-latest/tools.zip"


def _download_conditional(
	url: str,
	dest_path: Path,
	etag_path: Path,
	timeout: int = 30,
) -> bool:
	dest_path, etag_path = Path(dest_path), Path(etag_path)
	etag = None
	if dest_path.is_file() and etag_path.is_file():
		try:
			etag = etag_path.read_text(encoding="utf-8").strip()
		except OSError:
			etag = None

	headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) VanillaBuilder/1.0"}
	if etag and dest_path.is_file() and dest_path.stat().st_size > 0:
		headers["If-None-Match"] = etag

	req = urllib.request.Request(url, headers=headers)
	try:
		with urllib.request.urlopen(req, timeout=timeout) as resp:
			new_etag = resp.headers.get("ETag")
			dest_path.parent.mkdir(parents=True, exist_ok=True)
			temp_file = dest_path.with_suffix(dest_path.suffix + ".download.tmp")
			with open(temp_file, "wb") as out_file:
				shutil.copyfileobj(resp, out_file)
			temp_file.replace(dest_path)
			if new_etag:
				try:
					etag_path.write_text(new_etag.strip(), encoding="utf-8")
				except OSError:
					pass
			return True
	except urllib.error.HTTPError as err:
		if err.code == 304:
			return False
		if err.code == 404:
			raise
		return False
	except Exception:
		return False


def _download_and_merge_game_hashes(
	hashes_dir: Path,
	force: bool = False,
) -> None:
	unified_game = hashes_dir / "hashes.game.txt"
	if not force and unified_game.is_file() and unified_game.stat().st_size > 1024 * 1024:
		return

	parts_downloaded: list[Path] = []
	any_part_updated = False
	part_idx = 0

	while True:
		url = f"https://raw.githubusercontent.com/CommunityDragon/Data/master/hashes/lol/hashes.game.txt.{part_idx}"
		part_path = hashes_dir / f"hashes.game.txt.{part_idx}.part"
		etag_path = hashes_dir / f"hashes.game.txt.{part_idx}.etag"
		if force and etag_path.exists():
			etag_path.unlink(missing_ok=True)
		try:
			updated = _download_conditional(url, part_path, etag_path)
			if updated:
				any_part_updated = True
			parts_downloaded.append(part_path)
			part_idx += 1
		except Exception:
			break

	needs_merge = any_part_updated or not unified_game.is_file() or unified_game.stat().st_size < 1024
	if parts_downloaded and needs_merge:
		temp_unified = hashes_dir / "hashes.game.txt.tmp"
		with open(temp_unified, "wb") as out_final:
			for p in parts_downloaded:
				if p.is_file():
					with open(p, "rb") as f_in:
						shutil.copyfileobj(f_in, out_final)
		temp_unified.replace(unified_game)
		for p in parts_downloaded:
			p.unlink(missing_ok=True)
			p.with_suffix(".etag").unlink(missing_ok=True)
	elif not parts_downloaded:
		monolithic_url = "https://raw.githubusercontent.com/CommunityDragon/Data/master/hashes/lol/hashes.game.txt"
		etag_mono = hashes_dir / "hashes.game.txt.etag"
		if force and etag_mono.exists():
			etag_mono.unlink(missing_ok=True)
		try:
			_download_conditional(monolithic_url, unified_game, etag_mono)
		except Exception:
			pass


def ensure_ritobin_hashes(
	hashes_dir: Path,
	force: bool = False,
) -> None:
	hashes_dir = Path(hashes_dir)
	hashes_dir.mkdir(parents=True, exist_ok=True)
	for filename, url in _COMMUNITY_DRAGON_RITOBIN_URLS.items():
		dest = hashes_dir / filename
		etag_path = hashes_dir / f"{filename}.etag"
		if force and etag_path.exists():
			etag_path.unlink(missing_ok=True)
		try:
			_download_conditional(url, dest, etag_path)
		except Exception:
			pass
	_download_and_merge_game_hashes(hashes_dir, force=force)


def ensure_toolkit_binaries(
	tools_dir: Path,
	force: bool = False,
	progress_callback: Callable[[str], None] | None = None,
) -> None:
	tools_dir = Path(tools_dir)
	tools_dir.mkdir(parents=True, exist_ok=True)

	wadtools_path = tools_dir / "wadtools.exe"
	ritobin_path = tools_dir / "ritobin.exe"

	zip_dest = tools_dir / "tools.zip"
	etag_path = tools_dir / "tools.zip.etag"

	if force and etag_path.exists():
		etag_path.unlink(missing_ok=True)

	try:
		if progress_callback:
			progress_callback("Verifying LTK tools (CI/CD)...")

		updated = _download_conditional(LTK_TOOLS_ZIP_URL, zip_dest, etag_path, timeout=35)

		if updated or not wadtools_path.is_file() or not ritobin_path.is_file():
			if zip_dest.is_file():
				if progress_callback:
					progress_callback("Extracting updated LTK binaries...")
				with zipfile.ZipFile(zip_dest, "r") as archive:
					for member in archive.infolist():
						filename = Path(member.filename).name
						if filename.lower() in ("wadtools.exe", "ritobin.exe"):
							target_file = tools_dir / filename
							with archive.open(member) as source, open(target_file, "wb") as target:
								shutil.copyfileobj(source, target)
				zip_dest.unlink(missing_ok=True)
				if progress_callback:
					progress_callback("LTK tools synchronized successfully.")
		else:
			if progress_callback:
				progress_callback("LTK tools verified (cached).")
	except Exception as exc:
		if not wadtools_path.is_file() or not ritobin_path.is_file():
			sys.stderr.write(f"Failed to obtain tools from {LTK_TOOLS_ZIP_URL}: {exc}\n")


def run_silent_cmd(cmd: list[str]) -> subprocess.CompletedProcess:
	kwargs: dict = {}
	if sys.platform == "win32":
		kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
		startupinfo = subprocess.STARTUPINFO()
		startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
		startupinfo.wShowWindow = subprocess.SW_HIDE
		kwargs["startupinfo"] = startupinfo
	return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs)


# ==============================================================================
# RITOBIN RUST / C++ DYNAMIC CLI ADAPTER (PR #284 COMPATIBLE)
# ==============================================================================
_RITOBIN_IS_RUST_CACHE: dict[Path, bool] = {}


def is_rust_ritobin(ritobin_exe: Path) -> bool:
	if ritobin_exe in _RITOBIN_IS_RUST_CACHE:
		return _RITOBIN_IS_RUST_CACHE[ritobin_exe]
	try:
		res = run_silent_cmd([str(ritobin_exe), "--help"])
		output = (res.stdout or "") + (res.stderr or "")
		is_rust = "convert" in output.lower()
	except Exception:
		is_rust = False
	_RITOBIN_IS_RUST_CACHE[ritobin_exe] = is_rust
	return is_rust


def build_ritobin_cmd(
	ritobin_exe: Path,
	mode: str,
	input_path: Path,
	output_path: Path,
	hashes_dir: Path | None = None,
) -> list[str]:
	if is_rust_ritobin(ritobin_exe):
		cmd = [
			str(ritobin_exe),
			"convert",
			str(input_path),
			"-o", str(output_path),
			"-t", "rito" if mode == "decompile" else "bin",
		]
		if hashes_dir and Path(hashes_dir).is_dir():
			cmd += ["-H", str(hashes_dir)]
		return cmd
	else:
		cmd = [
			str(ritobin_exe),
			"-i", "bin" if mode == "decompile" else "text",
			"-o", "text" if mode == "decompile" else "bin",
		]
		if hashes_dir and Path(hashes_dir).is_dir():
			cmd += ["-d", str(hashes_dir)]
		cmd += [str(input_path), str(output_path)]
		return cmd


def decompile_single_bin_job(args: tuple[Path, Path, Path, Path | None]) -> bool:
	bin_path, out_txt, ritobin_exe, hashes_d = args
	cmd = build_ritobin_cmd(ritobin_exe, "decompile", bin_path, out_txt, hashes_d)
	res = run_silent_cmd(cmd)
	return res.returncode == 0 and out_txt.is_file() and out_txt.stat().st_size > 0


def decompile_bin_job(b: Path) -> None:
	cmd_dec = build_ritobin_cmd(RITOBIN, "decompile", b, b.with_suffix(b.suffix + ".txt"), HASHES_DIR)
	run_silent_cmd(cmd_dec)


def recompile_txt_job(txt_file: Path) -> tuple[Path | None, str]:
	out_bin = txt_file.with_name(txt_file.name[:-4])
	cmd_rec = build_ritobin_cmd(RITOBIN, "recompile", txt_file, out_bin, HASHES_DIR)
	res = run_silent_cmd(cmd_rec)
	err_msg = res.stderr or res.stdout or ""
	return (out_bin if res.returncode == 0 and out_bin.is_file() else None, err_msg)


def sync_template_linked_block(template_content: str, base_text: str) -> str:
	if not base_text:
		return template_content
	m_base = re.search(r'(?i)(linked:\s*list\[string\]\s*=\s*\{[\s\S]*?\n\})', base_text)
	if not m_base:
		return template_content
	live_linked_block = m_base.group(1)
	return re.sub(r'(?i)linked:\s*list\[string\]\s*=\s*\{[\s\S]*?\n\}', live_linked_block, template_content, count=1)


def fast_vanilla_revert(
	base_text: str,
	target_skin: str,
	skin_numeric_id: str | None,
	entity: str = "",
	main_entity: str = "",
) -> str:
	text = re.sub(
		r'(?i)(["\'](?:Characters[/\\])?[^"\'/\\]+[/\\]Skins[/\\])Skin\d+(["\']\s*=\s*SkinCharacterDataProperties)',
		rf'\g<1>{target_skin}\g<2>',
		base_text,
		count=1,
	)
	text = re.sub(
		r'(?i)(objectPath:\s*hash(?:\[[^\]]+\])?\s*=\s*["\'](?:Characters[/\\])?[^"\'/\\]+[/\\]Skins[/\\])Skin\d+(["\'])',
		rf'\g<1>{target_skin}\g<2>',
		text,
		count=1,
	)
	text, n_res_defs = re.subn(
		r'(?i)(["\'](?:Characters[/\\])?[^"\'/\\]+[/\\]Skins[/\\])Skin\d+([/\\]Resources["\']\s*=\s*ResourceResolver)',
		rf'\g<1>{target_skin}\g<2>',
		text,
		count=1,
	)
	if n_res_defs > 0:
		text = re.sub(
			r'(?i)(mResourceResolver:\s*link\s*=\s*["\'](?:Characters[/\\])?[^"\'/\\]+[/\\]Skins[/\\])Skin\d+([/\\]Resources["\'])',
			rf'\g<1>{target_skin}\g<2>',
			text,
			count=1,
		)

	if skin_numeric_id and skin_numeric_id.isdigit():
		text = re.sub(r'(?i)(\b(?:m_?)?skinId\s*:\s*(?:u32|int|u16)\s*=\s*)\d+', rf'\g<1>{skin_numeric_id}', text, count=1)
		text = re.sub(r'(?i)(\bchampionSkinId\s*:\s*i32\s*=\s*)\d+', rf'\g<1>{skin_numeric_id}', text, count=1)

	return text


def patch_skn_submesh_inplace(base_skn_bytes: bytes, old_name: str, new_name: str) -> bytes:
	old_name_bytes = old_name.encode("utf-8")
	new_name_bytes = new_name.encode("utf-8")[:63]

	idx = base_skn_bytes.find(old_name_bytes + b"\x00")
	if idx == -1:
		idx = base_skn_bytes.find(old_name_bytes)

	if idx == -1:
		return base_skn_bytes

	new_name_padded = new_name_bytes + b"\x00" * (64 - len(new_name_bytes))
	patched = bytearray(base_skn_bytes)
	patched[idx : idx + 64] = new_name_padded
	return bytes(patched)


def resolve_champion_template(workspace_root: Path, entity: str, skin_stem: str) -> Path | None:
	overrides_dir = Path(workspace_root) / "overrides"
	candidates = [
		overrides_dir / entity.lower() / "skins" / f"{skin_stem.lower()}.bin.txt",
		overrides_dir / entity.lower() / f"{skin_stem.lower()}.bin.txt",
		overrides_dir / "data" / "characters" / entity.lower() / "skins" / f"{skin_stem.lower()}.bin.txt",
	]
	for candidate in candidates:
		if candidate.is_file() and candidate.stat().st_size > 0:
			return candidate
	return None


def extract_resource_map_from_text(content: str) -> dict[str, str]:
	res_map: dict[str, str] = {}
	m_block = re.search(r'ResourceResolver\s*\{[\s\S]*?resourceMap:\s*map\[hash(?:\[[^\]]+\])?,\s*link\]\s*=\s*\{([\s\S]*?)\}\s*\}', content)
	if not m_block:
		return res_map

	raw_lines = m_block.group(1).splitlines()
	for line in raw_lines:
		line = line.strip()
		m_line = re.match(r'^(?:"([^"]+)"|([^\s=]+))\s*=\s*"([^"]+)"', line)
		if m_line:
			key = m_line.group(1) or m_line.group(2)
			val = m_line.group(3)
			res_map[key] = val
	return res_map


def extract_resource_map_raw_snippet(content: str) -> str:
	m_block = re.search(r'(["\'][^"\']+["\']\s*=\s*ResourceResolver\s*\{[\s\S]*?resourceMap:\s*map\[hash(?:\[[^\]]+\])?,\s*link\]\s*=\s*\{[\s\S]*?\}\s*\})', content)
	if m_block:
		return m_block.group(1).strip()
	return ""


def extract_satellite_preloads(content: str) -> list[str]:
	m_preloads = re.search(r'(?i)extraCharacterPreloads:\s*list\[string\]\s*=\s*\{([\s\S]*?)\}', content)
	if not m_preloads:
		return []
	return [s.lower() for s in re.findall(r'"([^"]+)"', m_preloads.group(1))]


def extract_parent_skin_id(content: str) -> str | None:
	m_parent = re.search(r'(?i)(?:chromaParentSkinId|parentSkinId)\s*:\s*(?:u32|int)\s*=\s*(\d+)', content)
	if m_parent:
		return str(int(m_parent.group(1)))
	m_skl = re.search(r'(?i)skeleton:\s*string\s*=\s*"[^"]*?[/\\]Skin(\d+)[/\\]', content)
	if m_skl:
		return str(int(m_skl.group(1)))
	return None


def _fnv1a(s: str) -> int:
	h = 0x811c9dc5
	for b in s.encode("utf-8"):
		h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
	return h


_PARENT_NAMES = (
	"parentSkinId",
	"chromaParentSkinId",
	"skinParent",
	"mParentSkinId",
	"mChromaParentSkinId",
	"parentskinid",
	"chromaparentskinid",
)
_PARENT_PROP_HASHES = tuple(
	struct.pack("<I", _fnv1a(name))
	for name in _PARENT_NAMES
)


def extract_parent_skin_id_from_raw_bin(bin_path: Path) -> str | None:
	try:
		raw = bin_path.read_bytes()

		# 1. Fast detection: custom skeleton path (normalizes "02" -> "2")
		m_skl = re.search(rb'(?i)[/\\]Skins[/\\]Skin(\d+)[/\\][^/\\]+?\.skl', raw)
		if m_skl:
			return str(int(m_skl.group(1).decode("ascii")))

		# 2. Fast detection: binary property hash lookup for chroma parent id
		for h_bytes in _PARENT_PROP_HASHES:
			idx = 0
			while True:
				pos = raw.find(h_bytes, idx)
				if pos == -1:
					break
				if pos + 9 <= len(raw):
					val_type = raw[pos + 4]
					# Riot PROP types: 7 = u32, 6 = i32, 5 = u16, 3 = u8
					if val_type in (6, 7):
						val = struct.unpack_from("<I", raw, pos + 5)[0]
						if 0 < val < 1000:
							return str(val)
					elif val_type in (4, 5):
						val = struct.unpack_from("<H", raw, pos + 5)[0]
						if 0 < val < 1000:
							return str(val)
					elif val_type in (2, 3):
						val = struct.unpack_from("<B", raw, pos + 5)[0]
						if 0 < val < 1000:
							return str(val)
				idx = pos + 4

		# 3. Fast detection: text-based fallback
		m_txt = re.search(rb'(?i)(?:chromaParentSkinId|parentSkinId)\s*:\s*(?:u32|int)\s*=\s*(\d+)', raw)
		if m_txt:
			return str(int(m_txt.group(1).decode("ascii")))
	except Exception:
		pass
	return None


def extract_skeleton_path(content: str) -> str:
	m = re.search(r'(?i)skeleton:\s*string\s*=\s*"([^"]+)"', content)
	return m.group(1).replace("\\", "/").strip() if m else ""


def extract_animgraph_path(content: str) -> str:
	m = re.search(r'(?i)animationGraphData:\s*link\s*=\s*"([^"]+)"', content)
	return m.group(1).replace("\\", "/").strip() if m else ""


def extract_idle_effect_key(content: str) -> str:
	m = re.search(r'(?i)SkinCharacterDataProperties_CharacterIdleEffect\s*\{[\s\S]*?effectKey:\s*hash(?:\[[^\]]+\])?\s*=\s*"([^"]+)"', content)
	return m.group(1) if m else ""


def read_skn_submeshes(skn_path: Path) -> list[str]:
	if not skn_path.is_file() or skn_path.stat().st_size < 12:
		return []
	try:
		with open(skn_path, "rb") as f:
			raw = f.read(2048)
		magic, version, num_submeshes = struct.unpack_from("<III", raw, 0)
		if magic != 0x00112233:
			return []
		names = []
		offset = 12
		for _ in range(min(num_submeshes, 32)):
			if offset + 64 > len(raw):
				break
			name_raw = raw[offset : offset + 64]
			name = name_raw.split(b"\x00", 1)[0].decode("utf-8", errors="ignore").strip()
			if name:
				names.append(name)
			offset += 80
		return names
	except Exception:
		return []


def inspect_rig_and_graph_divergence(skin_content: str, base_content: str) -> tuple[bool, bool, str, str]:
	base_skl = extract_skeleton_path(base_content).lower()
	skin_skl = extract_skeleton_path(skin_content)
	skin_skl_lower = skin_skl.lower()

	base_graph = extract_animgraph_path(base_content).lower()
	skin_graph = extract_animgraph_path(skin_content)
	skin_graph_lower = skin_graph.lower()

	is_base_skeleton = (not skin_skl_lower) or (skin_skl_lower == base_skl) or ("/base/" in skin_skl_lower) or ("/skin0/" in skin_skl_lower)
	is_base_graph = (not skin_graph_lower) or (skin_graph_lower == base_graph) or skin_graph_lower.endswith("/skin0")

	return not is_base_skeleton, not is_base_graph, skin_skl, skin_graph


def has_curated_template(entity: str, skin_name: str) -> bool:
	e_lower = entity.lower()
	s_lower = skin_name.lower()
	candidates = [
		OVERRIDES_DIR / e_lower / "skins" / f"{s_lower}.bin.txt",
		OVERRIDES_DIR / "data" / "characters" / e_lower / "skins" / f"{s_lower}.bin.txt",
		OVERRIDES_DIR / e_lower / f"{s_lower}.bin.txt",
	]
	for c in candidates:
		if c.is_file() and c.stat().st_size > 0:
			return True
	return False


def update_critical_report_log(champ_name: str, critical_items: list[dict], skin_filter: str) -> None:
	with REPORT_LOCK:
		existing_sections: dict[str, str] = {}
		if REPORT_FILE.is_file():
			try:
				raw_text = REPORT_FILE.read_text(encoding="utf-8")
				champ_blocks = re.findall(r'(?ms)## CHAMPION:\s*([A-Za-z0-9_]+)[^\n]*\n-+\n(.*?)(?=\n## CHAMPION:|\Z)', raw_text)
				for c_n, block_text in champ_blocks:
					existing_sections[c_n] = block_text.strip()
			except Exception:
				existing_sections = {}

		def format_skin_entry(s: dict) -> str:
			lines = [f"  * Skin ID: {s.get('skin', 'Unknown')} | Entity: {s.get('entity', 'Unknown')}"]
			skl = s.get("skeleton")
			graph = s.get("animation_graph")
			if skl and s.get("custom_skl"):
				lines.append(f"    - Custom Skeleton: {skl}")
			if graph and s.get("custom_graph"):
				lines.append(f"    - Custom AnimGraph: {graph}")
			if s.get("has_sub_forms"):
				lines.append("    - Dynamic State-Machine / Multi-Form")

			divergent = s.get("divergent_keys", [])
			if divergent:
				lines.append(f"    - Divergent Keys from Base ({len(divergent)}):")
				for k in divergent:
					lines.append(f"        • {k}")

			sub_div = s.get("submesh_divergence", [])
			if sub_div:
				lines.append(f"    - Submesh Contract Divergence ({len(sub_div)}):")
				for sd in sub_div:
					lines.append(f"        • Target Submesh: {sd}")
				if s.get("suggested_alias"):
					lines.append(f"        • Required Directive: # SUBMESH_ALIAS: {s['suggested_alias'][0]} -> {s['suggested_alias'][1]}")

			chromas = s.get("chromas", [])
			if chromas:
				lines.append(f"    - Covered Variants ({len(chromas)}): {', '.join(chromas)}")

			idle_eff = s.get("idle_effect", "")
			if idle_eff:
				lines.append(f"    - Persistent Idle Effect: {idle_eff}")

			return "\n".join(lines)

		current_block = existing_sections.get(champ_name, "")
		preserved_entries: list[str] = []

		if current_block:
			raw_entries = re.split(r'(?m)(?=^\s*\*\s*Skin ID:)', current_block)
			target_token = f"skin{skin_filter}".lower()

			for entry in raw_entries:
				entry_clean = entry.strip()
				if not entry_clean:
					continue
				m_skin = re.search(r'Skin ID:\s*Skin(\d+)', entry_clean, re.IGNORECASE)
				if m_skin and f"skin{m_skin.group(1)}".lower() == target_token:
					continue
				preserved_entries.append(entry_clean)

		new_entries = [format_skin_entry(s) for s in critical_items]
		combined_entries = preserved_entries + new_entries

		if combined_entries:
			existing_sections[champ_name] = "\n\n".join(combined_entries).strip()
		else:
			existing_sections.pop(champ_name, None)

		sorted_champ_names = sorted(existing_sections.keys(), key=lambda s: s.lower())

		output_lines = [
			"# ===========================================================================",
			"# VANILLA TRUE BUILDER — AUDIT REPORT LEDGER",
			f"# Last Updated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
			f"# Total Audited Champion Nodes: {len(sorted_champ_names)}",
			"# ===========================================================================\n",
		]

		for c_n in sorted_champ_names:
			body = existing_sections[c_n]
			output_lines.append(f"## CHAMPION: {c_n}")
			output_lines.append("-" * 75)
			output_lines.append(body)
			output_lines.append("\n")

		try:
			REPORT_FILE.write_text("\n".join(output_lines), encoding="utf-8")
		except Exception as exc:
			sys.stderr.write(f"Failed to write REPORT_FILE: {exc}\n")


class ChampionAuditorApp:
	C_TITLEBAR = "#0b1016"
	C_TITLEBAR_BORDER = "#1a2530"
	C_TITLEBAR_TEXT = "#ffffff"
	C_TITLEBAR_ICONS = "#7d8b99"
	C_TITLEBAR_HOVER = "#18202d"
	C_CLOSE_HOVER = "#c42b1c"

	C_BG_ROOT = "#080c12"
	C_BG_CARD = "#0f1823"
	C_BORDER_GOLD = "#785a28"
	C_BORDER_DARK = "#1a2530"
	C_GOLD_TEXT = "#f0e6d2"
	C_GOLD_ACCENT = "#c8aa6e"
	C_CYAN_MAGIC = "#0ac8b9"
	C_RED_ERROR = "#ff4655"
	C_YELLOW_WARN = "#ebeb5f"
	C_GREEN_SUCCESS = "#3cd070"
	C_TEXT_MUTED = "#7a8288"

	def __init__(self, root: tk.Tk):
		self.root = root
		self.root.title("Vanilla True Builder — AUDITOR")
		self.root.geometry("1040x720+160+70")
		self.root.minsize(980, 600)
		self.root.configure(bg=self.C_BG_ROOT)

		self.root.overrideredirect(True)
		self.is_maximized = False
		self._drag_data = {"x": 0, "y": 0}
		self._integrate_windows_taskbar()
		self.root.protocol("WM_DELETE_WINDOW", self.on_close)

		self.icon_photo = None
		icon_path = resolve_app_icon()
		if icon_path and icon_path.is_file():
			try:
				self.root.iconbitmap(str(icon_path))
				try:
					from PIL import Image, ImageTk
					pil_img = Image.open(str(icon_path)).resize((16, 16), Image.Resampling.LANCZOS)
					self.icon_photo = ImageTk.PhotoImage(pil_img)
				except Exception:
					self.icon_photo = None
			except Exception:
				pass

		self.champion_families: list[dict] = []
		self.skins_metadata: dict[str, list[dict]] = {}
		self.current_skin_map: dict[str, str] = {}

		self.has_active_audit: bool = False
		self.is_auditing: bool = False

		self.search_var = tk.StringVar()
		self.search_var.trace_add("write", lambda *args: self._render_results())

		self.status_var = tk.StringVar(value="WAITING ACTION")
		self.sort_state: dict[str, bool] = {}
		self._cursor_toast: tk.Toplevel | None = None
		self._context_pos = (0, 0)

		self._configure_styles()
		self._build_custom_titlebar()
		self._build_ui()
		self._populate_champion_list()

		self.root.bind("<FocusIn>", self._on_window_focus)
		threading.Thread(target=self._init_skins_metadata, daemon=True).start()

	def _on_window_focus(self, event=None):
		if event and event.widget != self.root:
			return

		self._cleanup_empty_overrides()

		selected_item = self._get_selected_item()
		if selected_item:
			t_file = OVERRIDES_DIR / selected_item["entity"].lower() / "skins" / f"skin{selected_item['skin_id']}.bin.txt"
			if t_file.is_file() and t_file.stat().st_size > 0:
				self.delete_champion_cache(selected_item["champ"])

		if self.has_active_audit and not self.is_auditing and self.champion_families:
			selected_nodes = self.tree.selection()
			self._render_results()
			if selected_nodes:
				try:
					self.tree.selection_set(selected_nodes)
				except Exception:
					pass

	def _populate_champion_list(self):
		if CHAMPIONS_DIR and CHAMPIONS_DIR.is_dir():
			valid_wads = sorted([p.name.replace(".wad.client", "") for p in CHAMPIONS_DIR.iterdir() if is_valid_champion_wad(p)])
			self.champ_combo["values"] = valid_wads
			if valid_wads:
				self.champ_combo.set(valid_wads[0])
				self._update_skin_list()

	def _integrate_windows_taskbar(self) -> None:
		if sys.platform == "win32":
			try:
				import ctypes
				ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("neve.vanillabuilder.auditor.1")
				self.root.update_idletasks()
				self.hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
				ex_style = ctypes.windll.user32.GetWindowLongW(self.hwnd, -20)
				ex_style = (ex_style | 0x00040000) & ~0x00000080
				ctypes.windll.user32.SetWindowLongW(self.hwnd, -20, ex_style)
				style = ctypes.windll.user32.GetWindowLongW(self.hwnd, -16)
				style = style | 0x00020000 | 0x00080000
				ctypes.windll.user32.SetWindowLongW(self.hwnd, -16, style)

				self.root.withdraw()
				self.root.after(10, self.root.deiconify)
			except Exception:
				pass

	def _build_custom_titlebar(self) -> None:
		self.title_bar = tk.Frame(self.root, bg=self.C_TITLEBAR, height=36)
		self.title_bar.pack(fill=tk.X, side=tk.TOP)
		self.title_bar.pack_propagate(False)

		border_line = tk.Frame(self.root, bg=self.C_TITLEBAR_BORDER, height=1)
		border_line.pack(fill=tk.X, side=tk.TOP)

		left_frame = tk.Frame(self.title_bar, bg=self.C_TITLEBAR)
		left_frame.pack(side=tk.LEFT, padx=12, pady=6)

		if self.icon_photo:
			self.logo_label = tk.Label(left_frame, image=self.icon_photo, bg=self.C_TITLEBAR)
			self.logo_label.image = self.icon_photo
			self.logo_label.pack(side=tk.LEFT, padx=(0, 6))
		else:
			self.logo_label = tk.Label(left_frame, text="✦", bg=self.C_TITLEBAR, fg=self.C_CYAN_MAGIC, font=("Segoe UI", 10, "bold"))
			self.logo_label.pack(side=tk.LEFT, padx=(0, 6))

		title = tk.Label(left_frame, text="Vanilla True Builder — AUDITOR", bg=self.C_TITLEBAR, fg=self.C_TITLEBAR_TEXT, font=("Segoe UI", 9, "bold"))
		title.pack(side=tk.LEFT)

		version = tk.Label(left_frame, text=APP_VERSION, bg=self.C_TITLEBAR, fg=self.C_TEXT_MUTED, font=("Segoe UI", 8))
		version.pack(side=tk.LEFT, padx=(6, 0))

		btn_frame = tk.Frame(self.title_bar, bg=self.C_TITLEBAR)
		btn_frame.pack(side=tk.RIGHT)

		self.btn_close = tk.Label(btn_frame, text="✕", bg=self.C_TITLEBAR, fg=self.C_TITLEBAR_ICONS, font=("Segoe UI", 9), width=5, height=2)
		self.btn_close.pack(side=tk.RIGHT)
		self.btn_close.bind("<Button-1>", lambda e: self.on_close())
		self.btn_close.bind("<Enter>", lambda e: self.btn_close.config(bg=self.C_CLOSE_HOVER, fg="#ffffff"))
		self.btn_close.bind("<Leave>", lambda e: self.btn_close.config(bg=self.C_TITLEBAR, fg=self.C_TITLEBAR_ICONS))

		self.btn_max = tk.Label(btn_frame, text="❐", bg=self.C_TITLEBAR, fg=self.C_TITLEBAR_ICONS, font=("Segoe UI", 9), width=5, height=2)
		self.btn_max.pack(side=tk.RIGHT)
		self.btn_max.bind("<Button-1>", lambda e: self.toggle_maximize())
		self.btn_max.bind("<Enter>", lambda e: self.btn_max.config(bg=self.C_TITLEBAR_HOVER, fg=self.C_GOLD_TEXT))
		self.btn_max.bind("<Leave>", lambda e: self.btn_max.config(bg=self.C_TITLEBAR, fg=self.C_TITLEBAR_ICONS))

		self.btn_min = tk.Label(btn_frame, text="—", bg=self.C_TITLEBAR, fg=self.C_TITLEBAR_ICONS, font=("Segoe UI", 9), width=5, height=2)
		self.btn_min.pack(side=tk.RIGHT)
		self.btn_min.bind("<Button-1>", lambda e: self.minimize())
		self.btn_min.bind("<Enter>", lambda e: self.btn_min.config(bg=self.C_TITLEBAR_HOVER, fg=self.C_GOLD_TEXT))
		self.btn_min.bind("<Leave>", lambda e: self.btn_min.config(bg=self.C_TITLEBAR, fg=self.C_TITLEBAR_ICONS))

		for widget in (self.title_bar, left_frame, self.logo_label, title, version):
			widget.bind("<ButtonPress-1>", self._start_move)
			widget.bind("<B1-Motion>", self._do_move)
			widget.bind("<Double-Button-1>", lambda e: self.toggle_maximize())

	def _start_move(self, event) -> None:
		self._drag_data["x"] = event.x_root - self.root.winfo_x()
		self._drag_data["y"] = event.y_root - self.root.winfo_y()

	def _do_move(self, event) -> None:
		if not self.is_maximized:
			x = event.x_root - self._drag_data["x"]
			y = event.y_root - self._drag_data["y"]
			self.root.geometry(f"+{x}+{y}")

	def toggle_maximize(self) -> None:
		if not self.is_maximized:
			self.prev_geo = self.root.geometry()

			if sys.platform == "win32":
				try:
					import ctypes
					from ctypes import wintypes

					class MONITORINFO(ctypes.Structure):
						_fields_ = [
							("cbSize", wintypes.DWORD),
							("rcMonitor", wintypes.RECT),
							("rcWork", wintypes.RECT),
							("dwFlags", wintypes.DWORD),
						]

					user32 = ctypes.windll.user32
					hwnd = getattr(self, "hwnd", None) or self.root.winfo_id()
					h_monitor = user32.MonitorFromWindow(hwnd, 2)

					mi = MONITORINFO()
					mi.cbSize = ctypes.sizeof(MONITORINFO)
					if user32.GetMonitorInfoW(h_monitor, ctypes.byref(mi)):
						work = mi.rcWork
						width = work.right - work.left
						height = work.bottom - work.top
						x = work.left
						y = work.top
						self.root.geometry(f"{width}x{height}+{x}+{y}")
						self.is_maximized = True
						return
				except Exception:
					pass

			self.root.geometry(f"{self.root.winfo_screenwidth()}x{self.root.winfo_screenheight()}+0+0")
			self.is_maximized = True
		else:
			self.root.geometry(self.prev_geo)
			self.is_maximized = False

	def minimize(self) -> None:
		if sys.platform == "win32" and hasattr(self, "hwnd") and self.hwnd:
			try:
				import ctypes
				ctypes.windll.ShowWindow(self.hwnd, 6)
				return
			except Exception:
				pass
		self.root.iconify()

	def on_close(self) -> None:
		self._cleanup_empty_overrides()
		if sys.platform == "win32":
			try:
				subprocess.run(
					["taskkill", "/F", "/T", "/PID", str(os.getpid())],
					creationflags=subprocess.CREATE_NO_WINDOW,
					stdout=subprocess.DEVNULL,
					stderr=subprocess.DEVNULL,
				)
			except Exception:
				pass
		os._exit(0)

	def _configure_styles(self):
		style = ttk.Style(self.root)
		try:
			style.theme_use("clam")
		except Exception:
			pass

		style.configure("Root.TFrame", background=self.C_BG_ROOT)
		style.configure("Card.TFrame", background=self.C_BG_CARD, relief="solid", borderwidth=1, bordercolor=self.C_BORDER_GOLD)
		style.configure("Title.TLabel", background=self.C_BG_ROOT, foreground=self.C_GOLD_ACCENT, font=("Segoe UI", 15, "bold"))
		style.configure("Credit.TLabel", background=self.C_BG_ROOT, foreground="#586675", font=("Segoe UI", 8))
		style.configure("Text.TLabel", background=self.C_BG_ROOT, foreground=self.C_GOLD_TEXT, font=("Segoe UI", 9))
		style.configure("Muted.TLabel", background=self.C_BG_ROOT, foreground=self.C_TEXT_MUTED, font=("Segoe UI", 8))

		style.configure(
			"HextechUtil.TButton",
			background=self.C_BG_ROOT,
			foreground=self.C_GOLD_ACCENT,
			font=("Segoe UI", 8, "bold"),
			borderwidth=1,
			bordercolor=self.C_BORDER_DARK,
			relief="solid",
			focuscolor=self.C_BG_ROOT,
			padding=(10, 2),
		)
		style.map(
			"HextechUtil.TButton",
			background=[("disabled", "#0b1016"), ("pressed", "#1f2c3a"), ("active", self.C_BG_CARD)],
			foreground=[("disabled", "#4b5664"), ("pressed", self.C_GOLD_TEXT), ("active", self.C_GOLD_TEXT)],
			bordercolor=[("disabled", "#141c24"), ("pressed", self.C_BORDER_GOLD), ("active", self.C_BORDER_GOLD)],
		)

		style.configure(
			"HextechAction.TButton",
			background="#16202c",
			foreground=self.C_GOLD_ACCENT,
			font=("Segoe UI", 9, "bold"),
			borderwidth=1,
			bordercolor=self.C_BORDER_GOLD,
			relief="solid",
			focuscolor="#16202c",
		)
		style.map(
			"HextechAction.TButton",
			background=[("disabled", "#0c1219"), ("active", "#253344")],
			foreground=[("disabled", "#55606c"), ("active", self.C_GOLD_TEXT)],
			bordercolor=[("disabled", "#1c2530"), ("active", self.C_BORDER_GOLD)],
		)

		style.configure(
			"Treeview",
			background="#05080c",
			foreground="#f0e6d2",
			fieldbackground="#05080c",
			font=("Consolas", 9),
			rowheight=25,
		)
		style.map("Treeview", background=[("selected", "#1a2c40")], foreground=[("selected", self.C_CYAN_MAGIC)])
		style.configure("Treeview.Heading", background="#121b27", foreground=self.C_GOLD_ACCENT, font=("Segoe UI", 9, "bold"))
		style.map("Treeview.Heading", background=[("active", "#182535")], foreground=[("active", self.C_CYAN_MAGIC)])

		style.configure("TCombobox", fieldbackground="#ffffff", foreground="#000000", font=("Segoe UI", 9, "bold"))
		style.map(
			"TCombobox",
			fieldbackground=[("disabled", "#dcdcdc"), ("readonly", "#ffffff"), ("!disabled", "#ffffff")],
			foreground=[("disabled", "#7a8288"), ("readonly", "#000000"), ("!disabled", "#000000")],
			selectbackground=[("readonly", "#ffffff"), ("!disabled", "#ffffff")],
			selectforeground=[("readonly", "#000000"), ("!disabled", "#000000")],
		)

		style.configure("TEntry", fieldbackground="#ffffff", foreground="#000000", font=("Segoe UI", 9))
		style.map(
			"TEntry",
			fieldbackground=[("disabled", "#dcdcdc"), ("!disabled", "#ffffff")],
			foreground=[("disabled", "#7a8288"), ("!disabled", "#000000")],
		)

	def show_cursor_toast(self, message: str = "✓ Copied!") -> None:
		if hasattr(self, "_cursor_toast") and self._cursor_toast and self._cursor_toast.winfo_exists():
			try:
				self._cursor_toast.destroy()
			except Exception:
				pass

		x, y = self._context_pos
		if x == 0 and y == 0:
			x = self.root.winfo_pointerx()
			y = self.root.winfo_pointery()

		toast = tk.Toplevel(self.root)
		toast.overrideredirect(True)
		toast.attributes("-topmost", True)
		toast.configure(bg="#16202c")

		frame = tk.Frame(toast, bg="#16202c", padx=8, pady=3)
		frame.pack()

		lbl = tk.Label(frame, text=message, bg="#16202c", fg="#c8aa6e", font=("Segoe UI", 8))
		lbl.pack()

		toast.geometry(f"+{x + 12}+{y - 24}")
		self._cursor_toast = toast

		def dismiss():
			try:
				if toast.winfo_exists():
					toast.destroy()
			except Exception:
				pass

		self.root.after(900, dismiss)

	def _build_ui(self):
		main_frame = ttk.Frame(self.root, style="Root.TFrame", padding=(16, 10, 16, 12))
		main_frame.pack(fill=tk.BOTH, expand=True)

		header = ttk.Frame(main_frame, style="Root.TFrame")
		header.pack(fill=tk.X, pady=(0, 10))

		header_left = ttk.Frame(header, style="Root.TFrame")
		header_left.pack(side=tk.LEFT, anchor="w")

		ttk.Label(header_left, text="Status", style="Title.TLabel").pack(anchor="w")

		self.status_badge = tk.Label(
			header_left,
			textvariable=self.status_var,
			bg=self.C_BG_ROOT,
			fg=self.C_CYAN_MAGIC,
			font=("Segoe UI", 8, "bold"),
		)
		self.status_badge.pack(anchor="w", pady=(2, 0))

		header_right = ttk.Frame(header, style="Root.TFrame")
		header_right.pack(side=tk.RIGHT, anchor="ne")
		ttk.Label(header_right, text="by Neve | RAKUZAN", style="Credit.TLabel").pack(anchor="e")

		toolbar = ttk.Frame(main_frame, style="Root.TFrame")
		toolbar.pack(fill=tk.X, pady=(0, 12))

		ttk.Label(toolbar, text="Champion:", style="Text.TLabel").pack(side=tk.LEFT, padx=(0, 6))
		self.champ_combo = ttk.Combobox(toolbar, width=17, state="readonly", font=("Segoe UI", 9, "bold"))
		self.champ_combo.pack(side=tk.LEFT, padx=(0, 10))
		self.champ_combo.bind("<<ComboboxSelected>>", lambda e: (self._update_skin_list(), e.widget.selection_clear()))
		self.champ_combo.bind("<FocusIn>", lambda e: e.widget.selection_clear())
		self.champ_combo.bind("<ButtonRelease-1>", lambda e: e.widget.selection_clear())

		ttk.Label(toolbar, text="Skin:", style="Text.TLabel").pack(side=tk.LEFT, padx=(0, 4))
		self.skin_filter_var = tk.StringVar(value="Select Skin")
		self.skin_combo = ttk.Combobox(toolbar, textvariable=self.skin_filter_var, width=32, state="readonly", font=("Segoe UI", 9, "bold"))
		self.skin_combo.pack(side=tk.LEFT, padx=(0, 10))
		self.skin_combo.bind("<<ComboboxSelected>>", lambda e: e.widget.selection_clear())
		self.skin_combo.bind("<FocusIn>", lambda e: e.widget.selection_clear())
		self.skin_combo.bind("<ButtonRelease-1>", lambda e: e.widget.selection_clear())

		self.search_label = tk.Label(toolbar, text="Search:", bg=self.C_BG_ROOT, fg="#7a8288", font=("Segoe UI", 9))
		self.search_label.pack(side=tk.LEFT, padx=(0, 6))

		self.search_entry = ttk.Entry(toolbar, textvariable=self.search_var, width=16, font=("Segoe UI", 9), style="TEntry", state="disabled")
		self.search_entry.pack(side=tk.LEFT, padx=(0, 14))

		self.btn_audit_single = ttk.Button(toolbar, text="AUDIT", style="HextechUtil.TButton", command=self.start_audit_single)
		self.btn_audit_single.pack(side=tk.LEFT, padx=(0, 6))

		self.btn_restore_skin = ttk.Button(toolbar, text="TEST", style="HextechUtil.TButton", command=self.build_audited_skin)
		self.btn_restore_skin.pack(side=tk.LEFT, padx=(0, 6))

		self.btn_guide = ttk.Button(toolbar, text="HELP", style="HextechUtil.TButton", command=self.open_workflow_guide)
		self.btn_guide.pack(side=tk.RIGHT)

		table_card = ttk.Frame(main_frame, style="Card.TFrame", padding=1)
		table_card.pack(fill=tk.BOTH, expand=True, pady=(0, 12))

		columns = ("champ", "skin", "entity", "chromas", "missing_keys")
		self.tree = ttk.Treeview(table_card, columns=columns, show="headings", selectmode="browse")

		self.tree.heading("champ", text="Node", command=lambda: self.sort_column("champ"), anchor="center")
		self.tree.heading("skin", text="Skin ID", command=lambda: self.sort_column("skin"), anchor="center")
		self.tree.heading("entity", text="Entity", command=lambda: self.sort_column("entity"), anchor="center")
		self.tree.heading("chromas", text="Variants", command=lambda: self.sort_column("chromas"), anchor="center")
		self.tree.heading("missing_keys", text="Divergent Keys", command=lambda: self.sort_column("missing_keys"), anchor="center")

		self.tree.column("champ", width=130, minwidth=100, stretch=False, anchor="center")
		self.tree.column("skin", width=100, minwidth=80, stretch=False, anchor="center")
		self.tree.column("entity", width=160, minwidth=130, stretch=False, anchor="center")
		self.tree.column("chromas", width=130, minwidth=100, stretch=False, anchor="center")
		self.tree.column("missing_keys", width=280, minwidth=200, stretch=True, anchor="center")

		self.tree.tag_configure("critical", foreground="#f0e6d2")
		self.tree.tag_configure("curated", foreground="#3cd070")

		scrollbar = ttk.Scrollbar(table_card, orient=tk.VERTICAL, command=self.tree.yview)
		self.tree.configure(yscrollcommand=scrollbar.set)
		scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
		self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

		self.tree.bind("<<TreeviewSelect>>", self.on_select_row)
		self.tree.bind("<Button-3>", self.show_context_menu)
		self.tree.bind("<Double-1>", self.on_double_click_row)

		self.context_menu = tk.Menu(self.root, tearoff=0, bg=self.C_BG_CARD, fg=self.C_GOLD_TEXT, activebackground="#16202c", activeforeground=self.C_CYAN_MAGIC, font=("Segoe UI", 9))
		self.context_menu.add_command(label="Open override file in editor", command=self.open_template_file)
		self.context_menu.add_command(label="Copy payload", command=self.prepare_template_and_copy_payload)

		footer_frame = ttk.Frame(main_frame, style="Root.TFrame")
		footer_frame.pack(fill=tk.X, pady=(2, 0))

		ttk.Label(
			footer_frame,
			text="Right-click row to copy payload. Double-click row to open file in editor.",
			style="Muted.TLabel",
		).pack(side=tk.LEFT, anchor="w", pady=2)

		self.btn_copy_report = ttk.Button(
			footer_frame,
			text="📋 COPY REPORT",
			style="HextechAction.TButton",
			command=self.copy_full_report,
			state=tk.DISABLED,
			takefocus=False,
		)
		self.btn_copy_report.pack(side=tk.RIGHT, ipady=4, ipadx=30)

	def _trigger_button_feedback(self, button: ttk.Button, temp_text: str, default_text: str, duration_ms: int = 1400) -> None:
		button.config(text=temp_text)
		button.update_idletasks()
		def restore():
			try:
				if button.winfo_exists():
					button.config(text=default_text)
					button.update_idletasks()
			except Exception:
				pass
		self.root.after(duration_ms, restore)

	def _init_skins_metadata(self) -> None:
		if SKINS_METADATA_FILE.is_file():
			try:
				self.skins_metadata = json.loads(SKINS_METADATA_FILE.read_text(encoding="utf-8"))
				self.root.after(0, self._update_skin_list)
			except Exception:
				pass

		try:
			req_ver = urllib.request.Request(
				"https://ddragon.leagueoflegends.com/api/versions.json",
				headers={"User-Agent": "VanillaBuilder/1.0"}
			)
			with urllib.request.urlopen(req_ver, timeout=10) as resp:
				latest_ver = json.loads(resp.read().decode("utf-8"))[0]

			current_ver = ""
			if SKINS_VERSION_FILE.is_file():
				current_ver = SKINS_VERSION_FILE.read_text(encoding="utf-8").strip()

			if current_ver == latest_ver and SKINS_METADATA_FILE.is_file():
				return

			url_data = f"https://ddragon.leagueoflegends.com/cdn/{latest_ver}/data/en_US/championFull.json"
			req_data = urllib.request.Request(url_data, headers={"User-Agent": "VanillaBuilder/1.0"})
			with urllib.request.urlopen(req_data, timeout=25) as resp:
				raw_data = json.loads(resp.read().decode("utf-8"))

			catalog: dict[str, list[dict]] = {}
			for c_id, c_info in raw_data.get("data", {}).items():
				skins = []
				for s in c_info.get("skins", []):
					s_name = s.get("name", "Default")
					if s_name.lower() in ("default", "base") or s.get("num") == 0:
						continue
					skins.append({"id": int(s.get("num", 0)), "name": s_name})
				catalog[c_id.lower()] = skins

			SKINS_METADATA_FILE.parent.mkdir(parents=True, exist_ok=True)
			SKINS_METADATA_FILE.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
			SKINS_VERSION_FILE.write_text(latest_ver, encoding="utf-8")

			self.skins_metadata = catalog
			self.root.after(0, self._update_skin_list)
		except Exception:
			pass

	def _update_skin_list(self) -> None:
		raw_champ = self.champ_combo.get().strip()
		champ_key = raw_champ.lower()
		self.current_skin_map.clear()

		if not raw_champ:
			self.skin_combo["values"] = ["Select Skin"]
			self.skin_filter_var.set("Select Skin")
			return

		skins = self.skins_metadata.get(champ_key, [])
		display_names = []

		if skins:
			for s in skins:
				full_name = s.get("name", "Skin")
				if full_name.lower() in ("default", "base") or s.get("id") == 0:
					continue

				clean_name = re.sub(rf'(?i)\b{re.escape(raw_champ)}\b', '', full_name)
				clean_name = re.sub(r'\s+', ' ', clean_name).strip()
				if not clean_name:
					clean_name = full_name

				display = clean_name
				self.current_skin_map[display] = str(s["id"])
				display_names.append(display)
		else:
			for i in range(1, 40):
				display = f"Skin {i}"
				self.current_skin_map[display] = str(i)
				display_names.append(display)

		display_names.sort(key=lambda x: x.lower())
		final_options = ["Select Skin"] + display_names

		self.skin_combo["values"] = final_options
		self.skin_filter_var.set("Select Skin")

	def _update_search_state(self) -> None:
		can_search = (not self.is_auditing) and self.has_active_audit and (len(self.champion_families) > 0)
		if can_search:
			self.search_entry.config(state="normal")
			self.search_label.config(fg=self.C_GOLD_TEXT)
		else:
			self.search_entry.config(state="disabled")
			self.search_label.config(fg="#7a8288")

	def delete_champion_cache(self, champ_name: str) -> list[str]:
		deleted = []
		champ_lower = champ_name.lower()
		cache_dirs = [
			WORKSPACE_ROOT / "cache",
			WORKSPACE_ROOT / "cache_wad",
			WORKSPACE_ROOT / "cache_wads",
		]

		for c_dir in cache_dirs:
			if not c_dir.is_dir():
				continue
			for item in c_dir.iterdir():
				item_name_lower = item.name.lower()
				stem = item_name_lower.split(".")[0]

				if stem == champ_lower or item_name_lower.startswith(f"{champ_lower}."):
					try:
						if item.is_file():
							item.unlink(missing_ok=True)
							deleted.append(item.name)
						elif item.is_dir():
							shutil.rmtree(item, ignore_errors=True)
							deleted.append(item.name)
					except Exception:
						pass
		return deleted

	def open_workflow_guide(self):
		guide_win = tk.Toplevel(self.root)
		guide_win.overrideredirect(True)
		guide_win.attributes("-topmost", True)
		guide_win.configure(bg=self.C_BG_ROOT)

		outer_border = tk.Frame(guide_win, bg="#223040", padx=1, pady=1)
		outer_border.pack(fill=tk.BOTH, expand=True)

		inner_container = tk.Frame(outer_border, bg=self.C_BG_ROOT)
		inner_container.pack(fill=tk.BOTH, expand=True)

		title_bar = tk.Frame(inner_container, bg=self.C_TITLEBAR, height=32)
		title_bar.pack(fill=tk.X, side=tk.TOP)
		title_bar.pack_propagate(False)

		title_line = tk.Frame(inner_container, bg=self.C_TITLEBAR_BORDER, height=1)
		title_line.pack(fill=tk.X, side=tk.TOP)

		title_frame = tk.Frame(title_bar, bg=self.C_TITLEBAR)
		title_frame.pack(side=tk.LEFT, padx=12)

		lbl_icon = tk.Label(title_frame, text="ⓘ ", bg=self.C_TITLEBAR, fg=self.C_CYAN_MAGIC, font=("Segoe UI", 9, "bold"))
		lbl_icon.pack(side=tk.LEFT)

		lbl_title = tk.Label(title_frame, text="AI Curation", bg=self.C_TITLEBAR, fg=self.C_GOLD_TEXT, font=("Segoe UI", 9, "bold"))
		lbl_title.pack(side=tk.LEFT)

		is_active = True

		def dismiss():
			nonlocal is_active
			if not is_active:
				return
			is_active = False
			try:
				if guide_win.winfo_exists():
					guide_win.destroy()
			except Exception:
				pass

		btn_close_guide = tk.Label(
			title_bar,
			text="✕",
			bg=self.C_TITLEBAR,
			fg=self.C_TITLEBAR_ICONS,
			font=("Segoe UI", 9),
			width=5,
			height=2,
			cursor="hand2",
		)
		btn_close_guide.pack(side=tk.RIGHT)
		btn_close_guide.bind("<Button-1>", lambda e: dismiss())
		btn_close_guide.bind("<Enter>", lambda e: btn_close_guide.config(bg=self.C_CLOSE_HOVER, fg="#ffffff"))
		btn_close_guide.bind("<Leave>", lambda e: btn_close_guide.config(bg=self.C_TITLEBAR, fg=self.C_TITLEBAR_ICONS))

		drag_offset = {"x": 0, "y": 0}

		def start_drag(e):
			drag_offset["x"] = e.x_root - guide_win.winfo_x()
			drag_offset["y"] = e.y_root - guide_win.winfo_y()

		def do_drag(e):
			guide_win.geometry(f"+{e.x_root - drag_offset['x']}+{e.y_root - drag_offset['y']}")

		for w_elem in (title_bar, title_frame, lbl_icon, lbl_title):
			w_elem.bind("<ButtonPress-1>", start_drag)
			w_elem.bind("<B1-Motion>", do_drag)

		guide_win.bind("<Escape>", lambda e: dismiss())

		if sys.platform == "win32":
			import ctypes

			class POINT(ctypes.Structure):
				_fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

			user32 = ctypes.windll.user32

			was_l_down = True
			was_r_down = True

			def monitor_screen_click():
				nonlocal was_l_down, was_r_down
				if not is_active:
					return
				try:
					if not guide_win.winfo_exists():
						return

					l_down = (user32.GetAsyncKeyState(0x01) & 0x8000) != 0
					r_down = (user32.GetAsyncKeyState(0x02) & 0x8000) != 0

					just_pressed = (l_down and not was_l_down) or (r_down and not was_r_down)

					was_l_down = l_down
					was_r_down = r_down

					if just_pressed:
						pt = POINT()
						user32.GetCursorPos(ctypes.byref(pt))

						gx = guide_win.winfo_rootx()
						gy = guide_win.winfo_rooty()
						gw = guide_win.winfo_width()
						gh = guide_win.winfo_height()

						if not (gx <= pt.x <= gx + gw and gy <= pt.y <= gy + gh):
							dismiss()
							return
				except Exception:
					pass

				if is_active:
					guide_win.after(20, monitor_screen_click)

			guide_win.after(150, monitor_screen_click)
		else:
			def check_click_outside(e):
				gx, gy = guide_win.winfo_rootx(), guide_win.winfo_rooty()
				gw, gh = guide_win.winfo_width(), guide_win.winfo_height()
				if not (gx <= e.x_root <= gx + gw and gy <= e.y_root <= gy + gh):
					dismiss()

			self.root.bind("<ButtonPress-1>", check_click_outside, add="+")
			guide_win.bind("<ButtonPress-1>", check_click_outside, add="+")

		body_frame = tk.Frame(inner_container, bg=self.C_BG_ROOT, padx=20, pady=16)
		body_frame.pack(fill=tk.BOTH, expand=True)

		def add_step(title: str, desc: str, is_last: bool = False):
			step_row = tk.Frame(body_frame, bg=self.C_BG_ROOT)
			step_row.pack(fill=tk.X, pady=(0, 0 if is_last else 10))

			s_left = tk.Frame(step_row, bg=self.C_BG_ROOT)
			s_left.pack(side=tk.LEFT)

			tk.Label(s_left, text=title, bg=self.C_BG_ROOT, fg=self.C_GOLD_ACCENT, font=("Segoe UI", 9, "bold")).pack(anchor="w")
			tk.Label(s_left, text=desc, bg=self.C_BG_ROOT, fg=self.C_TEXT_MUTED, font=("Segoe UI", 8)).pack(anchor="w")
			return step_row

		row_1 = add_step("1. Setup AI", "Click 'Copy Context' and paste it into any AI chat with reasoning capabilities.")

		def copybox():
			self.root.clipboard_clear()
			self.root.clipboard_append(get_ai_context())
			btn_copy.config(text="✓ Copied!")
			def reset_btn():
				try:
					if btn_copy.winfo_exists():
						btn_copy.config(text="📋 Copy Context")
				except Exception:
					pass
			guide_win.after(1400, reset_btn)

		btn_copy = ttk.Button(
			row_1,
			text="📋 Copy Context",
			style="HextechAction.TButton",
			command=copybox,
		)
		btn_copy.pack(side=tk.RIGHT, padx=(12, 0))

		add_step("2. Auditing", "Select the problematic champion skin, then click 'AUDIT' button, wait for the audit to finish.")
		add_step("3. Reporting", "Click 'COPY REPORT' below and paste into AI Chat, then wait AI tell you which row requires a payload.")
		add_step("4. Fixing", "Right-click only the row(s) requested by the AI to 'Copy payload', double-click it to paste the fix and save.")
		add_step("5. Finishing", "Build your .fantome package with champions module again.", is_last=True)

		guide_win.update_idletasks()
		req_w = max(500, guide_win.winfo_reqwidth())
		req_h = guide_win.winfo_reqheight()

		self.root.update_idletasks()
		rx = self.root.winfo_rootx()
		ry = self.root.winfo_rooty()
		rw = self.root.winfo_width()
		rh = self.root.winfo_height()

		x = max(0, rx + (rw - req_w) // 2)
		y = max(0, ry + (rh - req_h) // 2)
		guide_win.geometry(f"{req_w}x{req_h}+{x}+{y}")

		guide_win.lift()
		guide_win.focus_force()

	def _family_sort_key(self, item: dict):
		champ = item.get("champ", "").lower()
		try:
			skin_num = int(item.get("skin_id", 0))
		except (ValueError, TypeError):
			skin_num = 999999
		is_sat = 1 if item.get("is_satellite") else 0
		entity = item.get("entity", "").lower()
		return (champ, skin_num, is_sat, entity)

	def sort_column(self, col: str):
		rev = self.sort_state.get(col, False)
		self.sort_state[col] = not rev

		def get_sort_key(item: dict):
			if col == "champ":
				return (item["champ"].lower(), self._family_sort_key(item)[1:])
			elif col == "skin":
				try:
					return (int(item["skin_id"]), 1 if item.get("is_satellite") else 0)
				except ValueError:
					return (0, 0)
			elif col == "entity":
				return item["entity"].lower()
			elif col == "missing_keys":
				return len(item.get("divergent_keys", [])) + (50 if item.get("submesh_divergence") else 0)
			elif col == "chromas":
				return len(item.get("chromas", []))
			return self._family_sort_key(item)

		self.champion_families.sort(key=get_sort_key, reverse=rev)
		self._render_results()

	def _format_divergent_keys_display(self, threat: dict) -> str:
		divergent = threat.get("divergent_keys", [])
		count = len(divergent)

		details = []
		if threat.get("submesh_divergence"):
			details.append("Submesh Contract")
		if threat.get("has_sub_forms"):
			details.append("Multi-Form")
		if threat.get("custom_skl"):
			details.append("Skeleton")
		if threat.get("custom_graph"):
			details.append("AnimGraph")
		if threat.get("is_satellite") and threat.get("idle_effect"):
			details.append("Idle Effect")

		det_str = f"({', '.join(details)})" if details else ""
		if count > 0:
			return f"[{count}] {det_str}".strip()
		elif details:
			return f"[0] {det_str}".strip()
		return "[0] Identical to Base"

	def _audit_single_wad_internal(self, wad_path: Path, skin_filter: str) -> list[dict]:
		champ_name = wad_path.name.replace(".wad.client", "")
		critical_threats: list[dict] = []

		with tempfile.TemporaryDirectory(prefix=f"audit_{champ_name}_") as tmp_d_str:
			tmp_dir = Path(tmp_d_str)

			# Extract Skin Binaries AND 3D Mesh (.skn) files without restrictive -f bin filter
			extract_regex = rf"(?i)(skins/skin\d+\.bin$|skins/(?:base|skin0|skin{skin_filter})/.*\.skn$)"
			cmd_extract = [str(WADTOOLS), "extract", "-i", str(wad_path), "-o", str(tmp_dir), "--overwrite", "-x", extract_regex]
			hashes_game = HASHES_GAME if HASHES_GAME.is_file() else None
			if hashes_game and hashes_game.is_file():
				cmd_extract += ["-H", str(hashes_game)]
			run_silent_cmd(cmd_extract)

			bin_files = list(tmp_dir.rglob("*.bin"))
			if not bin_files:
				return []

			if not any(re.search(rf"[/\\]skin{skin_filter}\.bin$", p.as_posix(), re.IGNORECASE) for p in bin_files):
				raise ValueError(f"Skin ID {skin_filter} does not exist in {champ_name}.wad.client!")

			# Surgical decompile: only decompile Skin0 and target skin across entities
			target_bins = [
				b for b in bin_files
				if re.search(rf"[/\\]skin(?:0|{skin_filter})\.bin$", b.as_posix(), re.IGNORECASE)
			]
			decompile_jobs = [
				(b, b.with_suffix(b.suffix + ".txt"), RITOBIN, HASHES_DIR)
				for b in target_bins
			]
			with ThreadPoolExecutor(max_workers=MAX_PARALLEL_JOBS) as executor:
				list(executor.map(decompile_single_bin_job, decompile_jobs))

			# LOGIC TO MAP ALL ENTITIES
			entities_map: dict[str, dict[str, dict]] = {}
			for txt in tmp_dir.rglob("*.bin.txt"):
				rel = txt.relative_to(tmp_dir).as_posix().lower()
				parts = rel.split("/")
				if "characters" in parts:
					idx = parts.index("characters")
					if len(parts) > idx + 1:
						entity = parts[idx + 1]

						if entity.startswith("jade_") or "tft" in entity:
							continue

						stem = parts[-1].replace(".bin.txt", "").lower()
						m_s = re.fullmatch(r"skin(\d+)", stem)
						if m_s:
							skin_id = str(int(m_s.group(1)))
							try:
								content = txt.read_text(encoding="utf-8", errors="ignore")
							except Exception:
								continue

							res_map = extract_resource_map_from_text(content)
							raw_res = extract_resource_map_raw_snippet(content)
							satellites = extract_satellite_preloads(content)
							parent_id = extract_parent_skin_id(content)
							idle_key = extract_idle_effect_key(content)
							if entity not in entities_map:
								entities_map[entity] = {}
							entities_map[entity][skin_id] = {
								"content": content,
								"res_map": res_map,
								"raw_res": raw_res,
								"satellites": satellites,
								"parent_id": parent_id,
								"idle_effect": idle_key,
							}

			main_entity_name = champ_name.lower()
			main_entity_skins = entities_map.get(main_entity_name, {})
			base_main = main_entity_skins.get("0")

			# Read and compare Base and Target Skin SKN meshes
			skn_files = list(tmp_dir.rglob("*.skn"))
			base_submeshes = []
			target_submeshes = []

			for skn in skn_files:
				skn_str = skn.as_posix().lower()
				if "/base/" in skn_str or "/skin0/" in skn_str:
					if not base_submeshes:
						base_submeshes = read_skn_submeshes(skn)
				elif f"/skin{skin_filter}/" in skn_str or f"skin{skin_filter}.skn" in skn_str:
					if not target_submeshes:
						target_submeshes = read_skn_submeshes(skn)

			submesh_divergence = []
			suggested_alias = None
			if base_submeshes and target_submeshes:
				diff = [s for s in target_submeshes if s not in base_submeshes]
				if diff:
					submesh_divergence = diff
					suggested_alias = (base_submeshes[0], diff[0])

			# Fast chroma discovery scanning raw champion bins without ritobin overhead
			discovered_chromas: list[str] = []
			for b in bin_files:
				rel = b.relative_to(tmp_dir).as_posix().lower()
				parts = rel.split("/")
				if "characters" in parts:
					idx = parts.index("characters")
					if len(parts) > idx + 1 and parts[idx + 1] == main_entity_name:
						stem = b.stem.lower()
						m_s = re.fullmatch(r"skin(\d+)", stem)
						if m_s:
							s_id = str(int(m_s.group(1)))
							if s_id not in ("0", skin_filter):
								p_id = extract_parent_skin_id_from_raw_bin(b)
								if p_id == skin_filter:
									discovered_chromas.append(s_id)
			discovered_chromas.sort(key=int)

			for entity, skins in sorted(entities_map.items()):
				is_satellite_entity = (entity != main_entity_name)

				if skin_filter not in skins:
					continue

				skin0_info = skins.get("0") or base_main
				if not skin0_info:
					continue

				base_keys = set(skin0_info["res_map"].keys())
				base_content = skin0_info["content"]

				s_info = skins[skin_filter]
				skin_keys = set(s_info["res_map"].keys())
				satellites = s_info.get("satellites", [])

				divergent_keys = sorted(skin_keys - base_keys)
				custom_skl, custom_graph, skl_path, graph_path = inspect_rig_and_graph_divergence(s_info["content"], base_content)

				has_sub_forms = False
				if not is_satellite_entity:
					has_sub_forms = bool(re.search(r'(?i)(?:form_\d+|submodel|transformationbuff)', s_info["content"])) and (len(divergent_keys) > 0)

				critical_threats.append({
					"champ": champ_name,
					"skin": f"Skin{skin_filter}",
					"skin_id": skin_filter,
					"entity": entity,
					"is_satellite": is_satellite_entity,
					"skeleton": skl_path,
					"animation_graph": graph_path,
					"custom_skl": custom_skl,
					"custom_graph": custom_graph,
					"has_sub_forms": has_sub_forms,
					"divergent_keys": divergent_keys,
					"satellites": satellites,
					"chromas": discovered_chromas,
					"idle_effect": s_info.get("idle_effect", ""),
					"raw_res": s_info["raw_res"],
					"base_content": base_content,
					"submesh_divergence": submesh_divergence if not is_satellite_entity else [],
					"suggested_alias": suggested_alias if not is_satellite_entity else None,
				})

		critical_threats.sort(key=self._family_sort_key)
		update_critical_report_log(champ_name, critical_threats, skin_filter=skin_filter)
		return critical_threats

	def start_audit_single(self):
		champ = self.champ_combo.get().strip()
		selected_skin_name = self.skin_filter_var.get().strip()
		skin_filter = self.current_skin_map.get(selected_skin_name)

		if not skin_filter or selected_skin_name == "Select Skin":
			messagebox.showwarning("Warning", "Please select a skin to audit!", parent=self.root)
			return

		if not CHAMPIONS_DIR or not CHAMPIONS_DIR.is_dir():
			messagebox.showerror("Error", "Champions directory not found!", parent=self.root)
			return

		wad_target = CHAMPIONS_DIR / f"{champ}.wad.client"
		if not wad_target.is_file():
			wad_target = CHAMPIONS_DIR / f"{champ.capitalize()}.wad.client"

		if not wad_target or not wad_target.is_file():
			messagebox.showerror("Error", f"Could not find valid WAD for {champ} in Champions directory!", parent=self.root)
			return

		self.search_var.set("")
		self._set_ui_busy(True)
		self.status_var.set(f"AUDITING {champ.upper()} {selected_skin_name.upper()}...")
		self.status_badge.config(fg=self.C_YELLOW_WARN)
		self.champion_families.clear()
		self.tree.delete(*self.tree.get_children())
		self.btn_copy_report.config(state=tk.DISABLED)
		self.btn_restore_skin.config(state=tk.DISABLED)

		def worker():
			try:
				ensure_toolkit_binaries(TOOLS_DIR)
				ensure_ritobin_hashes(HASHES_DIR)
				res = self._audit_single_wad_internal(wad_target, skin_filter=skin_filter)
				self.champion_families = res
				self.has_active_audit = True

				def on_success():
					self._set_ui_busy(False)
					self._render_results()

				self.root.after(0, on_success)
			except Exception as exc:
				err_msg = str(exc)
				def on_error(m=err_msg):
					self._set_ui_busy(False)
					self.status_var.set("WAITING ACTION")
					self.status_badge.config(fg=self.C_CYAN_MAGIC)
					messagebox.showwarning("Audit Warning", m, parent=self.root)
				self.root.after(0, on_error)

		threading.Thread(target=worker, daemon=True).start()

	def _set_ui_busy(self, busy: bool):
		self.is_auditing = busy
		state = tk.DISABLED if busy else tk.NORMAL

		self.btn_audit_single.config(state=state)
		self.btn_restore_skin.config(state=state)
		if busy:
			self.champ_combo.config(state="disabled")
			self.skin_combo.config(state="disabled")
			self.search_entry.config(state="disabled")
			self.search_label.config(fg="#7a8288")
			self.btn_copy_report.config(state=tk.DISABLED)
			self.btn_restore_skin.config(state=tk.DISABLED)
		else:
			self.champ_combo.config(state="readonly")
			self.skin_combo.config(state="readonly")
			self._update_search_state()

	def _render_results(self):
		if self.is_auditing:
			return

		self.tree.delete(*self.tree.get_children())
		self.btn_copy_report.config(state=tk.DISABLED)
		self.btn_restore_skin.config(state=tk.DISABLED)
		query = self.search_var.get().strip().lower()

		displayed_count = 0

		for threat in self.champion_families:
			if query:
				searchable = f"{threat['champ']} {threat['skin']} {threat['entity']}".lower()
				if query not in searchable:
					continue

			displayed_count += 1

			crit_str = self._format_divergent_keys_display(threat)
			chroma_count = len(threat.get("chromas", []))
			chromas_str = f"[{chroma_count}]" if chroma_count > 0 else "N/A"

			is_curated = has_curated_template(threat["entity"], threat["skin"])
			row_tag = "curated" if is_curated else "critical"

			self.tree.insert(
				"",
				tk.END,
				values=(
					threat["champ"],
					threat["skin"],
					threat["entity"],
					chromas_str,
					crit_str
				),
				tags=(row_tag,),
			)

		if self.has_active_audit:
			raw_query = self.search_var.get().strip()
			if raw_query:
				if displayed_count > 0:
					self.status_var.set(f"FILTER: {displayed_count} MATCH(S)")
					self.status_badge.config(fg=self.C_CYAN_MAGIC)
				else:
					self.status_var.set(f"NO RESULTS FOR '{raw_query}'")
					self.status_badge.config(fg=self.C_RED_ERROR)
			else:
				self.status_var.set("AUDIT COMPLETE")
				self.status_badge.config(fg=self.C_GREEN_SUCCESS)

			btn_state = tk.NORMAL if self.champion_families else tk.DISABLED
			self.btn_copy_report.config(state=btn_state)
			self.btn_restore_skin.config(state=btn_state)
		else:
			self.status_var.set("WAITING ACTION")
			self.status_badge.config(fg=self.C_CYAN_MAGIC)

		self._update_search_state()

	def on_select_row(self, event):
		pass

	def on_double_click_row(self, event):
		row_id = self.tree.identify_row(event.y)
		if row_id:
			self.tree.selection_set(row_id)
			self.open_template_file()

	def show_context_menu(self, event):
		row_id = self.tree.identify_row(event.y)
		if not row_id:
			return
		self.tree.selection_set(row_id)
		item = self._get_selected_item()
		if not item:
			return
		self._context_pos = (event.x_root, event.y_root)
		self.context_menu.post(event.x_root, event.y_root)

	def _get_selected_item(self) -> dict | None:
		selected = self.tree.selection()
		if not selected:
			return None
		node_id = selected[0]
		vals = self.tree.item(node_id, "values")
		if not vals or len(vals) < 3:
			return None

		champ_name, skin_name, entity = str(vals[0]), str(vals[1]), str(vals[2])

		for item in self.champion_families:
			if (
				item["champ"].lower() == champ_name.lower()
				and item["skin"].lower() == skin_name.lower()
				and item["entity"].lower() == entity.lower()
			):
				return item

		return None

	def prepare_template_and_copy_payload(self):
		selected_nodes = self.tree.selection()
		item = self._get_selected_item()
		if not item:
			return

		self._cleanup_empty_overrides()

		entity = item["entity"].lower()
		skin_num = item["skin_id"]
		parent_champ = item["champ"]

		target_dir = OVERRIDES_DIR / entity / "skins"
		target_file = target_dir / f"skin{skin_num}.bin.txt"

		if target_file.is_file() and target_file.stat().st_size > 0:
			self.delete_champion_cache(parent_champ)

		target_dir.mkdir(parents=True, exist_ok=True)
		if not target_file.is_file():
			target_file.write_text("", encoding="utf-8")

		out_lines = [
			f"### CHAMPION CURATION PAYLOAD: {item['champ']} / {item['entity']} / {item['skin']}",
		]
		if item.get("is_satellite"):
			out_lines.append(f"* **Parent Champion:** `{item['champ']}`")

		if item.get("skeleton") and item.get("custom_skl"):
			out_lines.append(f"* **Custom Skeleton:** `{item['skeleton']}`")

		if item.get("animation_graph") and item.get("custom_graph"):
			out_lines.append(f"* **Custom AnimGraph:** `{item['animation_graph']}`")

		sub_div = item.get("submesh_divergence", [])
		if sub_div:
			out_lines.append(f"* **Submesh Contract Divergence Detected ({len(sub_div)}):**")
			for sd in sub_div:
				out_lines.append(f"  - Target Mesh Submesh: `{sd}`")
			if item.get("suggested_alias"):
				out_lines.append(f"* **Required Template Directive:** `# SUBMESH_ALIAS: {item['suggested_alias'][0]} -> {item['suggested_alias'][1]}`")

		divergent = item.get("divergent_keys", [])
		if divergent:
			out_lines.append(f"* **Divergent Keys from Base ({len(divergent)}):**")
			for k in divergent:
				out_lines.append(f"  - `{k}`")

		if item["chromas"] and not item.get("is_satellite"):
			out_lines.append(f"* **Covered Variants ({len(item['chromas'])}):** {', '.join(item['chromas'])}")

		if item.get("satellites"):
			out_lines.append(f"* **Satellite Preloads ({len(item['satellites'])}):**")
			for s in item["satellites"]:
				out_lines.append(f"  - `{s}`")

		if item.get("idle_effect"):
			out_lines.append(f"* **Persistent Idle Effect Key:** `{item['idle_effect']}`")

		if item["raw_res"]:
			out_lines.append(f"\n#### [TARGET SKIN RESOLVER: {item['skin']}]")
			out_lines.append("```ritobin")
			out_lines.append(item["raw_res"])
			out_lines.append("```")

		if item.get("base_content"):
			out_lines.append("\n#### [CANONICAL BASE FILE: Skin0.bin.txt]")
			out_lines.append("```ritobin")
			out_lines.append(item["base_content"])
			out_lines.append("```")

		payload = "\n".join(out_lines)
		self.root.clipboard_clear()
		self.root.clipboard_append(payload)

		if selected_nodes:
			try:
				self.tree.selection_set(selected_nodes)
			except Exception:
				pass

		self.show_cursor_toast("✓ Payload Copied!")

	def _cleanup_empty_overrides(self) -> None:
		if not OVERRIDES_DIR.is_dir():
			return
		for f in list(OVERRIDES_DIR.rglob("skin*.bin.txt")):
			if f.is_file() and f.stat().st_size == 0:
				try:
					f.unlink(missing_ok=True)
					skins_dir = f.parent
					if skins_dir.is_dir() and not any(skins_dir.iterdir()):
						skins_dir.rmdir()
						ent_dir = skins_dir.parent
						if ent_dir.is_dir() and not any(ent_dir.iterdir()):
							ent_dir.rmdir()
				except OSError:
					pass

	def open_template_file(self):
		item = self._get_selected_item()
		if not item:
			return
		entity = item["entity"].lower()
		skin_num = item["skin_id"]
		target_file = OVERRIDES_DIR / entity / "skins" / f"skin{skin_num}.bin.txt"
		if not target_file.is_file():
			target_file.parent.mkdir(parents=True, exist_ok=True)
			target_file.write_text("", encoding="utf-8")
		if sys.platform == "win32":
			os.startfile(str(target_file))

	def copy_full_report(self):
		if not self.champion_families:
			return

		champ_groups: dict[str, list[dict]] = {}
		for c in self.champion_families:
			champ_groups.setdefault(c["champ"], []).append(c)

		sorted_champs = sorted(champ_groups.keys(), key=lambda s: s.lower())

		output_lines = [
			"# ===========================================================================",
			"# VANILLA TRUE BUILDER — AUDIT REPORT",
			f"# Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
			f"# Scope: Family Node Telemetry (Main & Satellites)",
			"# ===========================================================================\n",
		]

		for c_name in sorted_champs:
			skins = champ_groups[c_name]
			output_lines.append(f"## CHAMPION: {c_name}")
			output_lines.append("-" * 75)
			for s in skins:
				output_lines.append(f"  * Skin ID: {s.get('skin', 'Unknown')} | Entity: {s.get('entity', 'Unknown')}")

				skl = s.get("skeleton")
				graph = s.get("animation_graph")
				if skl and s.get("custom_skl"):
					output_lines.append(f"    - Custom Skeleton: {skl}")
				if graph and s.get("custom_graph"):
					output_lines.append(f"    - Custom AnimGraph: {graph}")

				sub_div = s.get("submesh_divergence", [])
				if sub_div:
					output_lines.append(f"    - Submesh Contract Divergence ({len(sub_div)}):")
					for sd in sub_div:
						output_lines.append(f"        • Target Submesh: {sd}")
					if s.get("suggested_alias"):
						output_lines.append(f"        • Required Directive: # SUBMESH_ALIAS: {s['suggested_alias'][0]} -> {s['suggested_alias'][1]}")

				divergent = s.get("divergent_keys", [])
				if divergent:
					output_lines.append(f"    - Divergent Keys from Base ({len(divergent)}):")
					for k in divergent:
						output_lines.append(f"        • {k}")

				chromas = s.get("chromas", [])
				if chromas:
					output_lines.append(f"    - Covered Variants ({len(chromas)}): {', '.join(chromas)}")

				idle_eff = s.get("idle_effect", "")
				if idle_eff:
					output_lines.append(f"    - Persistent Idle Effect: {idle_eff}")

				output_lines.append("")
			output_lines.append("")

		payload = "\n".join(output_lines).strip()
		self.root.clipboard_clear()
		self.root.clipboard_append(payload)

		self._trigger_button_feedback(
			self.btn_copy_report,
			"✓ REPORT COPIED!",
			"📋 COPY REPORT"
		)

	def build_audited_skin(self):
		champ = self.champ_combo.get().strip()
		if not champ:
			messagebox.showwarning("Warning", "Please select a champion!", parent=self.root)
			return

		selected_skin_name = self.skin_filter_var.get().strip()
		skin_id = self.current_skin_map.get(selected_skin_name)
		if not skin_id or selected_skin_name == "Select Skin":
			messagebox.showwarning("Warning", "Please select a skin to test!", parent=self.root)
			return

		wad_target = CHAMPIONS_DIR / f"{champ}.wad.client"
		if not wad_target.is_file():
			wad_target = CHAMPIONS_DIR / f"{champ.capitalize()}.wad.client"

		if not wad_target.is_file():
			messagebox.showerror("Error", f"Could not find valid WAD for {champ}!", parent=self.root)
			return

		self._set_ui_busy(True)
		self.status_var.set(f"BUILDING {champ.upper()} {selected_skin_name.upper()}...")
		self.status_badge.config(fg=self.C_YELLOW_WARN)
		self.champion_families.clear()
		self.tree.delete(*self.tree.get_children())
		self.root.update_idletasks()
		self.has_active_audit = False
		self.btn_copy_report.config(state=tk.DISABLED)

		def worker():
			try:
				ensure_toolkit_binaries(TOOLS_DIR)
				ensure_ritobin_hashes(HASHES_DIR)

				with tempfile.TemporaryDirectory(prefix=f"vanilla_restore_{champ}_") as tmp_d_str:
					temp_dir = Path(tmp_d_str)

					# Discover Chroma Variants ONLY for Selected Champion (Parity with main.py)
					char_name = champ.lower()
					champ_tpl_dir = OVERRIDES_DIR / char_name / "skins"
					chroma_inheritance_map: dict[str, Path] = {}

					if champ_tpl_dir.is_dir():
						for tpl_file in champ_tpl_dir.glob("skin*.bin.txt"):
							if tpl_file.stat().st_size == 0:
								continue
							try:
								hdr = tpl_file.read_text(encoding="utf-8", errors="ignore")[:400]
								m_var = re.search(r'(?i)#\s*VARIANTS\s*:\s*([^\r\n]+)', hdr)
								if m_var:
									for raw_t in m_var.group(1).split(","):
										token = raw_t.strip().lower()
										if "-" in token:
											m_rng = re.match(r'(\d+)\s*-\s*(\d+)', token)
											if m_rng:
												for cid in range(int(m_rng.group(1)), int(m_rng.group(2)) + 1):
													chroma_inheritance_map[str(cid)] = tpl_file
										elif token.isdigit():
											chroma_inheritance_map[token] = tpl_file
							except OSError:
								pass

					target_skin_ids = {str(skin_id)}
					for cid, tpl_src in chroma_inheritance_map.items():
						if f"skin{skin_id}.bin.txt".lower() in tpl_src.name.lower():
							target_skin_ids.add(cid)

					# Extract WAD (Only Skin0, Targets and Base SKN)
					all_id_regex = "|".join(["0"] + sorted(list(target_skin_ids), key=int))
					extract_regex = rf"(?i)(skins/skin({all_id_regex})\.bin$|skins/base/.*\.skn$)"
					cmd_extract = [str(WADTOOLS), "extract", "-i", str(wad_target), "-o", str(temp_dir), "--overwrite", "-x", extract_regex]
					if HASHES_GAME.is_file():
						cmd_extract += ["-H", str(HASHES_GAME)]
					run_silent_cmd(cmd_extract)

					# Decompile base Skin0
					base_skin0_files = [
						p for p in temp_dir.rglob("*.bin")
						if p.stem.lower() == "skin0" and not any(tag in p.as_posix().lower() for tag in ("tft", "jade_"))
					]
					with ThreadPoolExecutor(max_workers=MAX_PARALLEL_JOBS) as executor:
						list(executor.map(decompile_bin_job, base_skin0_files))

					entries_to_pack: list[tuple[Path, str]] = []
					submesh_alias_tasks: list[tuple[str, str, str, str]] = []

					# Processing with Real Inheritance (Parity with main.py)
					matching_bins = [
						p for p in temp_dir.rglob("*.bin")
						if re.search(r'\d+', p.stem)
						and re.search(r'\d+', p.stem).group(0) in target_skin_ids
						and p.stem.lower() != "skin0"
						and not any(tag in p.as_posix().lower() for tag in ("tft", "jade_"))
					]

					for target_bin in matching_bins:
						parts = [pt.lower() for pt in target_bin.parts]
						char_idx = parts.index("characters") if "characters" in parts else -1
						c_name = parts[char_idx + 1] if char_idx >= 0 and len(parts) > char_idx + 1 else champ.lower()

						m_s = re.search(r'\d+', target_bin.stem)
						current_skin_num = m_s.group(0) if m_s else str(skin_id)
						target_skin = f"Skin{current_skin_num}"

						out_txt = target_bin.with_suffix(".bin.txt")

						template_file = resolve_champion_template(WORKSPACE_ROOT, c_name, f"skin{current_skin_num}")
						if not template_file and current_skin_num in chroma_inheritance_map:
							template_file = chroma_inheritance_map[current_skin_num]

						if template_file:
							raw_tpl = template_file.read_text(encoding="utf-8", errors="ignore")
							m_alias = re.search(r'(?i)#\s*SUBMESH_ALIAS\s*:\s*([^\s-]+)\s*->\s*([^\r\n]+)', raw_tpl)

							if m_alias:
								m_parent_skin = re.search(r'skin(\d+)', template_file.stem, re.IGNORECASE)
								parent_skin_sfx = m_parent_skin.group(1) if m_parent_skin else current_skin_num
								alias_tuple = (
									c_name,
									parent_skin_sfx,
									m_alias.group(1).strip(),
									m_alias.group(2).strip(),
								)
								if alias_tuple not in submesh_alias_tasks:
									submesh_alias_tasks.append(alias_tuple)

							clean_lines = [
								l for l in raw_tpl.splitlines()
								if not (l.strip().startswith("#") and not l.strip().startswith("#PROP"))
							]
							tpl_content = "\n".join(clean_lines)

							if c_name == champ.lower():
								base_f = target_bin.parent / "skin0.bin.txt"
								if base_f.is_file():
									try:
										live_base_text = base_f.read_text(encoding="utf-8", errors="ignore")
										tpl_content = sync_template_linked_block(tpl_content, live_base_text)
									except OSError:
										pass

							if current_skin_num and target_bin.stem.lower() not in template_file.name.lower():
								tpl_content = fast_vanilla_revert(tpl_content, target_skin, current_skin_num)

							out_txt.write_text(tpl_content, encoding="utf-8")
						else:
							base_txt = target_bin.parent / "skin0.bin.txt"
							if not base_txt.is_file():
								cands = list(target_bin.parent.glob("[sS][kK][iI][nN]0.bin.txt"))
								if cands:
									base_txt = cands[0]

							if base_txt.is_file():
								base_content = base_txt.read_text(encoding="utf-8")
								rev_content = fast_vanilla_revert(base_content, target_skin, current_skin_num)
								out_txt.write_text(rev_content, encoding="utf-8")
							else:
								continue

						recompiled_bin, rec_err = recompile_txt_job(out_txt)
						if recompiled_bin and recompiled_bin.is_file():
							rel_bin_path = recompiled_bin.relative_to(temp_dir).as_posix().lower()
							entries_to_pack.append((recompiled_bin, f"WAD/{wad_target.name}/{rel_bin_path}"))
						else:
							raise RuntimeError(f"Ritobin failed to recompile {out_txt.name}:\n{rec_err}")

					# Process In-Place Submesh Patch (Only for parent templates)
					if submesh_alias_tasks:
						for ent_name, skin_sfx, old_sub, new_sub in submesh_alias_tasks:
							base_skn_list = [p for p in temp_dir.rglob("*.skn") if "base" in p.as_posix().lower()]
							if not base_skn_list:
								base_skn_list = list(temp_dir.rglob(f"{ent_name}.skn")) + list(temp_dir.rglob(f"{ent_name.capitalize()}.skn"))

							if not base_skn_list:
								continue

							orig_skn_path = base_skn_list[0]
							raw_bytes = orig_skn_path.read_bytes()
							patched_skn_bytes = patch_skn_submesh_inplace(raw_bytes, old_sub, new_sub)

							patched_name = f"{ent_name.capitalize()}_Skin{skin_sfx}.skn"
							patched_skn_file = temp_dir / patched_name
							patched_skn_file.write_bytes(patched_skn_bytes)

							clean_rel_path = f"assets/characters/{ent_name}/skins/base/{patched_name}".lower()
							skn_arcname = f"WAD/{wad_target.name}/{clean_rel_path}"
							entries_to_pack.append((patched_skn_file, skn_arcname))

					if not entries_to_pack:
						raise RuntimeError(f"No files were compiled or packed for {champ} (Skin{skin_id})!")

					# 6. Package into .fantome Archive with Strict Lowercase Paths
					fantome_filename = f"VanillaTrue_{champ.capitalize()}_Skin{skin_id}.fantome"
					output_fantome = WORKSPACE_ROOT / fantome_filename

					metadata = {
						"Name": f"{champ} (Skin{skin_id}) — Vanilla",
						"Author": "Neve",
						"Version": APP_VERSION,
						"Description": f"Vanilla restored for: {champ} (Skin{skin_id}).",
						"License": "MIT License",
					}
					info_json_bytes = json.dumps(metadata, indent=2, ensure_ascii=False).encode("utf-8")

					readme_md = f"""### 🧪 Auditor Package
> Isolated build for **{champ.capitalize()} (Skin{skin_id})**.

Compiled via [Vanilla True Builder](https://github.com/gsmeven/vanilla-true-builder).
"""

					license_text = """Copyright (c) 2026 Neve / Vanilla True Builder

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

"""

					with zipfile.ZipFile(output_fantome, "w", compression=zipfile.ZIP_DEFLATED) as arc:
						arc.writestr("META/info.json", info_json_bytes)
						arc.writestr("META/README.md", readme_md.encode("utf-8"))
						arc.writestr("META/LICENSE", license_text.encode("utf-8"))
						for local_p, zip_p in entries_to_pack:
							parts = zip_p.split("/", 2)
							if len(parts) == 3 and parts[0] == "WAD":
								normalized_zip_p = f"WAD/{parts[1]}/{parts[2].lower()}"
							else:
								normalized_zip_p = zip_p.lower()
							arc.write(local_p, normalized_zip_p)

					def on_success(fn=fantome_filename, cnt=len(entries_to_pack)):
						self._set_ui_busy(False)
						self.status_var.set("BUILD COMPLETE")
						self.status_badge.config(fg=self.C_GREEN_SUCCESS)
						messagebox.showinfo(
							"Success",
							f"Build package generated successfully!\n\nFile: {fn}\n",
							parent=self.root,
						)

						self.status_var.set("WAITING ACTION")
						self.status_badge.config(fg=self.C_CYAN_MAGIC)

					self.root.after(0, on_success)
			except Exception as exc:
				err_str = str(exc)
				def on_error(m=err_str):
					self._set_ui_busy(False)
					self.status_var.set("BUILD FAILED")
					self.status_badge.config(fg=self.C_RED_ERROR)
					messagebox.showerror("Error", m, parent=self.root)

				self.root.after(0, on_error)

		threading.Thread(target=worker, daemon=True).start()


if __name__ == "__main__":
	tk_root = tk.Tk()
	app = ChampionAuditorApp(tk_root)
	tk_root.mainloop()