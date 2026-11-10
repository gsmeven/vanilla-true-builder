#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import logging
import multiprocessing
import os
import queue
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, as_completed, wait
from pathlib import Path
from typing import Callable

try:
	import tkinter as tk
	from tkinter import messagebox, ttk
except Exception:
	tk = None
	ttk = None

LOG = logging.getLogger("vanilla_builder")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

APP_VERSION = "v1.8.4"
GITHUB_REPO_URL = "https://github.com/gsmeven/vanilla-true-builder"
GITHUB_LATEST_API = "https://api.github.com/repos/gsmeven/vanilla-true-builder/releases/latest"

# ==============================================================================
# PIPELINE & CACHE CONFIGURATION
# ==============================================================================
if getattr(sys, "frozen", False):
	try:
		_curr_exe = Path(sys.executable).resolve()
		_old_exe = _curr_exe.with_name(_curr_exe.stem + "_old.exe")
		if _old_exe.is_file():
			_old_exe.unlink(missing_ok=True)
	except Exception:
		pass

def _parse_version(ver_str: str) -> tuple[int, ...]:
	clean = re.sub(r'^[^\d]*', '', ver_str.strip())
	parts = []
	for p in clean.split('.'):
		m = re.match(r'^\d+', p)
		parts.append(int(m.group(0)) if m else 0)
	return tuple(parts)

def is_newer_version(latest_str: str, current_str: str) -> bool:
	try:
		return _parse_version(latest_str) > _parse_version(current_str)
	except Exception:
		return latest_str.strip().lower() != current_str.strip().lower()

def perform_silent_auto_update(download_url: str, latest_tag: str, log_callback=None) -> bool:
	if not getattr(sys, "frozen", False):
		return False

	try:
		current_exe = Path(sys.executable).resolve()
		temp_dir = Path(tempfile.gettempdir()) / f"vtb_update_{int(time.time())}"
		temp_dir.mkdir(parents=True, exist_ok=True)
		zip_path = temp_dir / "update.zip"

		if log_callback:
			log_callback(f"Downloading new version ({latest_tag})...")

		req = urllib.request.Request(download_url, headers={"User-Agent": "VanillaBuilder-Updater/1.0"})
		with urllib.request.urlopen(req, timeout=30) as resp, open(zip_path, "wb") as f_out:
			shutil.copyfileobj(resp, f_out)

		new_exe_path = None
		with zipfile.ZipFile(zip_path, "r") as archive:
			for member in archive.infolist():
				if Path(member.filename).name.lower() == "vanillatruebuilder.exe":
					archive.extract(member, temp_dir)
					new_exe_path = temp_dir / member.filename
					break

		if not new_exe_path or not new_exe_path.is_file():
			return False

		if log_callback:
			log_callback("Restarting with the latest version...")

		old_exe = current_exe.with_name(current_exe.stem + "_old.exe")
		try:
			if old_exe.is_file():
				old_exe.unlink(missing_ok=True)
		except Exception:
			pass

		current_exe.rename(old_exe)
		shutil.move(str(new_exe_path), str(current_exe))

		subprocess.Popen([str(current_exe)] + sys.argv[1:], cwd=str(current_exe.parent))
		os._exit(0)
	except Exception as exc:
		LOG.warning("Silent auto-update failed: %s", exc)
		return False

RAW_TRANSPARENT_TEX_BYTES = (
	b"DDS \x7c\x00\x00\x00\x07\x10\x08\x00\x04\x00\x00\x00\x04\x00\x00\x00"
	b"\x10\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00\x00" + (b"\x00" * 44) +
	b"\x20\x00\x00\x00\x04\x00\x00\x00DXT5" + (b"\x00" * 20) +
	b"\x00\x10\x00\x00" + (b"\x00" * 16) +
	(b"\x00" * 16)
)

MAP11_TRANSPARENT_TEX_REL = "assets/transparent.tex"
GLOBAL_TRANSPARENT_TEX_REL = "assets/transparent.tex"


# ==============================================================================
# LEAGUE OF LEGENDS ENVIRONMENT DETECTION & WORKSPACE
# ==============================================================================
def detect_league_root() -> Path:
	default_path = Path(r"C:\Riot Games\League of Legends")
	if sys.platform == "win32":
		try:
			import winreg
			for root_key in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
				for key_path in (
					r"SOFTWARE\Riot Games, Inc\League of Legends",
					r"SOFTWARE\WOW6432Node\Riot Games, Inc\League of Legends",
				):
					try:
						with winreg.OpenKey(root_key, key_path) as key:
							val, _ = winreg.QueryValueEx(key, "Location")
							if val and Path(val).is_dir():
								return Path(val)
					except OSError:
						pass
		except Exception:
			pass

	import string
	for letter in string.ascii_uppercase:
		for sub_path in ("Riot Games/League of Legends", "League of Legends"):
			candidate = Path(f"{letter}:/{sub_path}")
			if candidate.is_dir() and (candidate / "Game").is_dir():
				return candidate

	return default_path


DEFAULT_LEAGUE_ROOT = detect_league_root()
DEFAULT_FINAL_DIR = DEFAULT_LEAGUE_ROOT / "Game" / "DATA" / "FINAL"
DEFAULT_MAP_WAD = DEFAULT_FINAL_DIR / "Maps" / "Shipping" / "Map11.wad.client"
DEFAULT_CHAMPIONS_DIR = DEFAULT_FINAL_DIR / "Champions"
CHAMPION_WAD_PATTERN = re.compile(r"^[a-zA-Z0-9]+\.wad\.client$", re.IGNORECASE)


def detect_ui_wad(final_dir: Path = DEFAULT_FINAL_DIR) -> Path | None:
	for name in ("UI.wad.client", "Global.wad.client"):
		candidate = final_dir / name
		if candidate.is_file() and candidate.stat().st_size > 1024:
			return candidate
	all_wads = list(final_dir.rglob("*.wad.client"))
	for p in all_wads:
		name = p.name.lower()
		if name in ("ui.wad.client", "global.wad.client") and p.stat().st_size > 1024:
			return p
	for p in all_wads:
		name = p.name.lower()
		if "ui" in name.lower() and not re.search(r"\.[a-z]{2}[_-][a-z]{2}\.", name.lower()):
			if p.stat().st_size > 5 * 1024 * 1024:
				return p
	return None


DEFAULT_UI_WAD = detect_ui_wad(DEFAULT_FINAL_DIR)

MAX_PARALLEL_WAD_JOBS = max(1, int((os.cpu_count() or 4) * 0.60))
MAX_PARALLEL_RITOBIN_JOBS = max(1, int((os.cpu_count() or 4) * 0.60))

if getattr(sys, "frozen", False):
	DEFAULT_WORKSPACE_ROOT = Path(sys.executable).resolve().parent
else:
	DEFAULT_WORKSPACE_ROOT = Path(__file__).resolve().parent

DEFAULT_TEMP_DIR = Path(tempfile.gettempdir()) / "VanillaTrueTemp"
DEFAULT_PROJECT_DIR = DEFAULT_TEMP_DIR / "VanillaTrue_Project"


def get_temp_workspace() -> Path:
	DEFAULT_TEMP_DIR.mkdir(parents=True, exist_ok=True)
	return DEFAULT_TEMP_DIR


def resolve_app_icon() -> Path | None:
	search_dirs: list[Path] = []
	if hasattr(sys, "_MEIPASS"):
		search_dirs.append(Path(getattr(sys, "_MEIPASS")))
	search_dirs.append(DEFAULT_WORKSPACE_ROOT)
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
# VANILLA GAME INTEGRITY RULES & CENTRALIZED REGEX DEFINITIONS
# ==============================================================================
def _rule(stem: str, target_canonical: str) -> tuple[re.Pattern, Callable[[re.Match], str]]:
	pattern_stem = rf'[\w-]*{re.escape(stem)}\w*'
	pattern = re.compile(
		rf'(["\'][^"\']*[/\\]{pattern_stem}["\'][^}}]*?texturePath:\s*file\s*=\s*)[^\r\n]+|'
		rf'(["\'])[^"\']*(?:[/\\]|^){pattern_stem}\.tex\2',
		flags=re.IGNORECASE
	)

	def _hybrid_replacer(match: re.Match) -> str:
		if match.group(1):
			return f'{match.group(1)}"{target_canonical}"'
		quote = match.group(2)
		return f'{quote}{target_canonical}{quote}'

	return (pattern, _hybrid_replacer)


_SRS_TO_CANONICAL_TEX_RULES: tuple[tuple[re.Pattern, Callable[[re.Match], str]], ...] = (
	_rule("Ground_C3_MidLane", "assets/maps/kitpieces/srx/base/textures/terrain_midlane_ground_a.tex"),
	_rule("Ground_A1_AlcoveTop", "assets/maps/kitpieces/srs/base/textures/ground_a1_alcovetop_a.tex"),
	_rule("Ground_E5_AlcoveBot", "assets/maps/kitpieces/srs/base/textures/ground_e5_alcovebot_a.tex"),
	_rule("Ground_D1_ChaosBase", "assets/maps/kitpieces/srs/base/textures/ground_d1_chaosbase_a.tex"),
	_rule("Ground_D2_ChaosBase", "assets/maps/kitpieces/srs/base/textures/ground_d2_chaosbase_a.tex"),
	_rule("Ground_E1_ChaosBase", "assets/maps/kitpieces/srs/base/textures/ground_e1_chaosbase_a.tex"),
	_rule("Ground_E2_ChaosBase", "assets/maps/kitpieces/srs/base/textures/ground_e2_chaosbase_a.tex"),
	_rule("Ground_A4_OrderBase", "assets/maps/kitpieces/srs/base/textures/ground_a4_orderbase_a.tex"),
	_rule("Ground_A5_OrderBase", "assets/maps/kitpieces/srs/base/textures/ground_a5_orderbase_a.tex"),
	_rule("Ground_B4_OrderBase", "assets/maps/kitpieces/srs/base/textures/ground_b4_orderbase_a.tex"),
	_rule("Ground_B5_OrderBase", "assets/maps/kitpieces/srs/base/textures/ground_b5_orderbase_a.tex"),
)

_ACCENT_OMISSION_PATTERNS = (
	re.compile(r"(?i)plinths?"),
	re.compile(r"(?i)hall_of_legends?"),
	re.compile(r"(?i)(?<![a-z0-9])hol(?![a-z0-9])"),
)

_RECALL_VFX_PATTERN = re.compile(r'(?i)(?<![a-z0-9])Recall_(?:Honor|Challenger)\w*')

_OTHER_INTEGRITY = {
	"ha",
	"bw",
	"bilgewater",
	"aram",
}

_TARGET_TYPES = {
	"vfxsystemdefinitiondata",
	"staticmaterialdef",
}

STRUCTURE_CHARACTERS = {
	"turret",
	"turretrubble",
	"inhibitor",
	"nexus",
	"sru_storekeepernorth",
	"sru_storekeepersouth",
}

MINION_CHARACTERS = {
	"sru_orderminionmelee",
	"sru_orderminionranged",
	"sru_orderminionsiege",
	"sru_orderminionsuper",
	"sru_chaosminionmelee",
	"sru_chaosminionranged",
	"sru_chaosminionsiege",
	"sru_chaosminionsuper",
}

WARD_CHARACTERS = {
	"sightward",
	"yellowtrinket",
	"jammerdevice",
	"bluetrinket",
}

MAP_CHAMPION_CHARACTERS = {
	"aniviaegg",
	"azirultsoldier",
	"elisespiderling",
	"fizzbait",
	"fizzshark",
	"lulupolymorphcritter",
	"malzaharvoidling",
	"shacobox",
	"syndraorbs",
	"syndrasphere",
	"taliyahwallchunk",
	"trundlewall",
	"viktorsingularity",
	"zedshadow",
	"zyraseed",
}

EVENT_CHARACTERS = {
	"hol_clickable",
	"npc_worldscup",
	"npc_khazixstatue",
}

BANNER_CHARACTERS = {
	"sru_es_bannerplatform_chaos",
	"sru_es_bannerplatform_order",
	"sru_es_bannerwall_chaos",
	"sru_es_bannerwall_order",
	"sruap_flag",
	"srx_banner_hero",
	"srx_banner_horizontal",
	"srx_banner_vertical",
	"srx_banner_verticalthin",
}


# ==============================================================================
# ATOMIC CANONICAL CACHE MANAGEMENT
# ==============================================================================
def _script_fingerprint() -> str:
	return hashlib.sha256(APP_VERSION.encode("utf-8")).hexdigest()[:16]


def file_fingerprint(path: Path) -> str:
	try:
		stat = Path(path).stat()
		raw_sig = f"{Path(path).resolve().as_posix().lower()}:{stat.st_size}:{int(stat.st_mtime_ns)}".encode("utf-8")
		return hashlib.sha256(raw_sig).hexdigest()
	except OSError:
		return "0" * 64


def get_canonical_cache_dir(cache_root: Path, wad_path: Path) -> Path:
	return Path(cache_root) / wad_path.name


def _read_cache_hashes(wad_dir: Path) -> tuple[str, str]:
	hash_file = wad_dir / ".hash"
	if not hash_file.is_file():
		return "", ""
	try:
		content = hash_file.read_text(encoding="utf-8").strip()
		parts = content.split(":", 1)
		return (parts[0], parts[1]) if len(parts) == 2 else (parts[0], "")
	except OSError:
		return "", ""


def _is_cache_valid(wad_dir: Path, current_wad_hash: str, current_script_hash: str) -> bool:
	saved_wad, saved_script = _read_cache_hashes(wad_dir)
	if not (saved_wad and saved_wad == current_wad_hash and saved_script == current_script_hash):
		return False
	return any(f.name != ".hash" and not f.name.endswith(".txt") for f in wad_dir.iterdir() if f.is_file() or f.is_dir())


def _write_cache_hash(wad_dir: Path, current_wad_hash: str, current_script_hash: str) -> None:
	wad_dir.mkdir(parents=True, exist_ok=True)
	(wad_dir / ".hash").write_text(f"{current_wad_hash}:{current_script_hash}", encoding="utf-8")


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
	progress_callback: Callable[[str], None] | None = None,
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
			if progress_callback:
				progress_callback(f"Updated: {dest_path.name}")
			return True
	except urllib.error.HTTPError as err:
		if err.code == 304:
			if progress_callback:
				progress_callback(f"Verified (cached): {dest_path.name}")
			return False
		if err.code == 404:
			raise
		LOG.warning("HTTP %d downloading %s. Preserving local copy.", err.code, url)
		return False
	except Exception as exc:
		LOG.warning("Network failure downloading %s: %s", url, exc)
		return False


def _download_and_merge_game_hashes(
	hashes_dir: Path,
	force: bool = False,
	progress_callback: Callable[[str], None] | None = None,
) -> None:
	unified_game = hashes_dir / "hashes.game.txt"
	if not force and unified_game.is_file() and unified_game.stat().st_size > 1024 * 1024:
		if progress_callback:
			progress_callback("Verified (local): hashes.game.txt")
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
			updated = _download_conditional(url, part_path, etag_path, progress_callback=progress_callback)
			if updated:
				any_part_updated = True
			if part_path.is_file() and part_path.stat().st_size > 0:
				parts_downloaded.append(part_path)
			part_idx += 1
		except urllib.error.HTTPError as err:
			if err.code == 404:
				break
		except Exception:
			break

	needs_merge = any_part_updated or not unified_game.is_file() or unified_game.stat().st_size < 1024
	if parts_downloaded and needs_merge:
		if progress_callback:
			progress_callback("Merging game hash chunks into unified container...")
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
		if progress_callback:
			progress_callback("Merged: hashes.game.txt")
	elif not parts_downloaded:
		monolithic_url = "https://raw.githubusercontent.com/CommunityDragon/Data/master/hashes/lol/hashes.game.txt"
		etag_mono = hashes_dir / "hashes.game.txt.etag"
		if force and etag_mono.exists():
			etag_mono.unlink(missing_ok=True)
		try:
			_download_conditional(monolithic_url, unified_game, etag_mono, progress_callback=progress_callback)
		except Exception:
			pass


def ensure_ritobin_hashes(
	hashes_dir: Path,
	force: bool = False,
	progress_callback: Callable[[str], None] | None = None,
) -> None:
	hashes_dir = Path(hashes_dir)
	hashes_dir.mkdir(parents=True, exist_ok=True)
	for filename, url in _COMMUNITY_DRAGON_RITOBIN_URLS.items():
		dest = hashes_dir / filename
		etag_path = hashes_dir / f"{filename}.etag"
		if force and etag_path.exists():
			etag_path.unlink(missing_ok=True)
		try:
			_download_conditional(url, dest, etag_path, progress_callback=progress_callback)
		except Exception as exc:
			LOG.warning("Failed to update %s: %s", filename, exc)
	_download_and_merge_game_hashes(hashes_dir, force=force, progress_callback=progress_callback)


def ensure_toolkit_binaries(
	tools_dir: Path,
	force: bool = False,
	progress_callback: Callable[[str], None] | None = None,
) -> None:
	tools_dir = Path(tools_dir)
	tools_dir.mkdir(parents=True, exist_ok=True)

	wadtools_path = tools_dir / "wadtools.exe"
	ritobin_path = tools_dir / "ritobin.exe"
	leaguemod_path = tools_dir / "league-mod.exe"

	zip_dest = tools_dir / "tools.zip"
	etag_path = tools_dir / "tools.zip.etag"

	if force and etag_path.exists():
		etag_path.unlink(missing_ok=True)

	try:
		if progress_callback:
			progress_callback("Verifying LTK tools (CI/CD)...")

		updated = _download_conditional(LTK_TOOLS_ZIP_URL, zip_dest, etag_path, timeout=35)

		if updated or not wadtools_path.is_file() or not ritobin_path.is_file() or not leaguemod_path.is_file():
			if zip_dest.is_file():
				if progress_callback:
					progress_callback("Extracting updated LTK binaries...")
				with zipfile.ZipFile(zip_dest, "r") as archive:
					for member in archive.infolist():
						filename = Path(member.filename).name
						if filename.lower() in ("wadtools.exe", "ritobin.exe", "league-mod.exe"):
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
			LOG.warning("Failed to obtain tools from %s: %s", LTK_TOOLS_ZIP_URL, exc)
		else:
			LOG.info("Using existing local tools (offline or unmodified).")


def build_ritobin_cmd(
	ritobin_exe: Path,
	mode: str,
	input_path: Path,
	output_path: Path,
	hashes_dir: Path | None = None,
) -> list[str]:
	cmd = [
		str(ritobin_exe),
		"convert",
		str(input_path),
		"-o", str(output_path),
		"-t", "rito" if mode == "decompile" else "bin",
	]
	if hashes_dir and Path(hashes_dir).is_dir() and any(Path(hashes_dir).glob("*.txt")):
		cmd += ["-H", str(hashes_dir)]
	return cmd


def run_cmd(
	cmd: list[str],
	cwd: Path | None = None,
	dry_run: bool = False,
	progress_callback: Callable[[str], None] | None = None,
) -> subprocess.CompletedProcess:
	if dry_run:
		return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

	kwargs: dict = {}
	if sys.platform == "win32":
		kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
		startupinfo = subprocess.STARTUPINFO()
		startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
		startupinfo.wShowWindow = subprocess.SW_HIDE
		kwargs["startupinfo"] = startupinfo

	process = subprocess.Popen(
		cmd,
		cwd=cwd,
		stdout=subprocess.PIPE,
		stderr=subprocess.STDOUT,
		text=True,
		encoding="utf-8",
		errors="replace",
		**kwargs,
	)
	output: list[str] = []
	assert process.stdout is not None
	for line in process.stdout:
		line = line.rstrip()
		if line:
			output.append(line)
			if progress_callback:
				progress_callback(line)
	returncode = process.wait()
	if returncode:
		detail = "\n".join(output)
		raise subprocess.CalledProcessError(returncode, cmd, output=detail, stderr=detail)

	return subprocess.CompletedProcess(cmd, returncode, stdout="\n".join(output), stderr="")


def extract_wad(
	wad_path: Path,
	dest_dir: Path,
	tools: dict,
	filter_type: str | None = "bin",
	filter_pattern: str | None = None,
	dry_run: bool = False,
	progress_callback: Callable[[str], None] | None = None,
) -> Path:
	wad_path, dest_dir = Path(wad_path), Path(dest_dir)
	dest_dir.mkdir(parents=True, exist_ok=True)

	if not tools.get("wadtools") or not tools["wadtools"].exists():
		raise FileNotFoundError(f"wadtools.exe not found for {wad_path.name}")

	cmd = [str(tools["wadtools"]), "extract", "-i", str(wad_path), "-o", str(dest_dir), "--overwrite"]
	if filter_type:
		cmd += ["-f", filter_type]
	if filter_pattern:
		cmd += ["-x", filter_pattern]

	hashes_game = Path(tools.get("hashes_dir", "")) / "hashes.game.txt"
	if hashes_game.is_file() and hashes_game.stat().st_size > 1024:
		cmd += ["-H", str(hashes_game)]

	try:
		run_cmd(cmd, dry_run=dry_run, progress_callback=progress_callback)
	except subprocess.CalledProcessError:
		if filter_pattern:
			fallback_cmd = [str(tools["wadtools"]), "extract", "-i", str(wad_path), "-o", str(dest_dir), "--overwrite"]
			if filter_type:
				fallback_cmd += ["-f", filter_type]
			if hashes_game.is_file() and hashes_game.stat().st_size > 1024:
				fallback_cmd += ["-H", str(hashes_game)]
			run_cmd(fallback_cmd, dry_run=dry_run, progress_callback=progress_callback)
		else:
			raise

	return dest_dir


def _ritobin_file_job(args: tuple[str, Path, Path, Path, Path | None, bool]) -> tuple[bool, str, str]:
	mode, input_path, output_path, ritobin, hashes_dir, dry_run = args
	cmd = build_ritobin_cmd(ritobin, mode, input_path, output_path, hashes_dir)
	try:
		run_cmd(cmd, dry_run=dry_run)
		return True, str(input_path), ""
	except subprocess.CalledProcessError as exc:
		raw_err = exc.stderr if exc.stderr else exc.stdout
		detail = raw_err.decode("utf-8", errors="ignore") if isinstance(raw_err, bytes) else str(raw_err or exc)
		return False, str(input_path), detail


def decompile_bin_files(
	source_dir: Path,
	tools: dict,
	dry_run: bool = False,
	progress_callback: Callable[[int, int, str], None] | None = None,
	file_selector: Callable[[Path], list[Path]] | None = None,
	parallel: bool = True,
) -> int:
	ritobin = tools.get("ritobin")
	hashes_dir = tools.get("hashes_dir")
	if not ritobin or not Path(ritobin).exists():
		raise FileNotFoundError(f"ritobin.exe not found for decompilation in {source_dir}")
	count = 0
	bin_files = file_selector(source_dir) if file_selector else find_files_by_extensions(source_dir, [".bin", ".materials"])
	total = len(bin_files)
	if not total:
		if progress_callback:
			progress_callback(0, 0, "decompile_complete")
		return 0
	jobs = []
	for bin_file in bin_files:
		out_txt = bin_file.with_name(bin_file.name + ".txt")
		jobs.append(("decompile", bin_file, out_txt, Path(ritobin), hashes_dir, dry_run))

	def consume(result, current_count: int):
		nonlocal count
		success, input_name, detail = result
		if success:
			count += 1
			if progress_callback:
				rel_name = str(Path(input_name).relative_to(source_dir)).replace("\\", "/")
				progress_callback(current_count, total, rel_name)
		else:
			raise RuntimeError(f"ritobin failed to decompile {input_name}: {detail or 'no details'}")

	if parallel and len(jobs) > 1:
		with ProcessPoolExecutor(max_workers=min(MAX_PARALLEL_RITOBIN_JOBS, len(jobs))) as executor:
			future_map = {executor.submit(_ritobin_file_job, job): idx for idx, job in enumerate(jobs, start=1)}
			completed_items = 0
			for future in as_completed(future_map):
				completed_items += 1
				consume(future.result(), completed_items)
	else:
		for idx, job in enumerate(jobs, start=1):
			consume(_ritobin_file_job(job), idx)

	return count


def recompile_text_to_bin(
	source_dir: Path,
	tools: dict,
	dry_run: bool = False,
	progress_callback: Callable[[int, int, str], None] | None = None,
	file_selector: Callable[[Path], list[Path]] | None = None,
	parallel: bool = True,
) -> int:
	ritobin = tools.get("ritobin")
	hashes_dir = tools.get("hashes_dir")
	if not ritobin or not Path(ritobin).exists():
		raise FileNotFoundError(f"ritobin.exe not found for recompilation in {source_dir}")
	count = 0
	txt_files = file_selector(source_dir) if file_selector else find_files_by_extensions(source_dir, ".txt")
	total = len(txt_files)
	if not total:
		return 0
	jobs = []
	for txt_file in txt_files:
		out_bin = txt_file.with_name(txt_file.name[:-4]) if txt_file.name.lower().endswith(".txt") else txt_file
		jobs.append(("recompile", txt_file, out_bin, Path(ritobin), hashes_dir, dry_run))

	def consume(result, current_count: int):
		nonlocal count
		success, input_name, detail = result
		if success:
			count += 1
			if progress_callback:
				rel_name = str(Path(input_name).relative_to(source_dir)).replace("\\", "/")
				progress_callback(current_count, total, rel_name)
		else:
			raise RuntimeError(f"ritobin failed to recompile {input_name}: {detail or 'no details'}")

	if parallel and len(jobs) > 1:
		with ProcessPoolExecutor(max_workers=min(MAX_PARALLEL_RITOBIN_JOBS, len(jobs))) as executor:
			future_map = {executor.submit(_ritobin_file_job, job): idx for idx, job in enumerate(jobs, start=1)}
			completed_items = 0
			for future in as_completed(future_map):
				completed_items += 1
				consume(future.result(), completed_items)
	else:
		for idx, job in enumerate(jobs, start=1):
			consume(_ritobin_file_job(job), idx)

	return count


# ==============================================================================
# FILE FILTERING & CANONICAL MASTER DISCOVERY
# ==============================================================================
def _path_has_segments(path: Path, *segments: str) -> bool:
	lowered = {part.lower() for part in Path(path).parts}
	return all(segment.lower() in lowered for segment in segments)


def _is_character_skin_path(path: Path) -> bool:
	parts = [part.lower() for part in path.parts]
	try:
		idx = parts.index("characters")
	except ValueError:
		return False
	if len(parts) <= idx + 2 or parts[idx + 2] != "skins":
		return False
	char_name = parts[idx + 1]
	if char_name.startswith("jade_") or "tft" in char_name:
		return False
	return bool(re.fullmatch(r"skin(?:-1|\d+|-[a-z0-9]+)\.bin", path.name, re.IGNORECASE))


def _iter_champion_files(root: Path) -> list[Path]:
	files: list[Path] = []
	seen: set[Path] = set()
	for current, _, filenames in os.walk(str(root)):
		current_path = Path(current)
		if not _path_has_segments(current_path, "characters"):
			continue
		for filename in filenames:
			path = current_path / filename
			if path.suffix.lower() != ".bin":
				continue
			if _is_character_skin_path(path) and path not in seen:
				seen.add(path)
				files.append(path)
	return sorted(files)


def _iter_target_champion_files(root: Path) -> list[Path]:
	return sorted(path for path in _iter_champion_files(root) if path.stem.lower() != "skin0")


def _iter_map11_master_vital_bins(root: Path) -> list[Path]:
	all_target_chars = STRUCTURE_CHARACTERS | MINION_CHARACTERS | WARD_CHARACTERS | MAP_CHAMPION_CHARACTERS
	vitals: list[Path] = []

	for current, dirnames, filenames in os.walk(str(root)):
		dirnames[:] = [d for d in dirnames if d.lower() not in {"animations", "spells", "audio", "sounds"}]
		current_path = Path(current)
		rel_parts = [p.lower() for p in current_path.parts]

		for filename in filenames:
			file_lower = filename.lower()
			if not file_lower.endswith(".bin"):
				continue

			p = current_path / filename

			if file_lower in {"base_srx.materials.bin"}:
				vitals.append(p)
				continue

			if "characters" in rel_parts:
				char_name = None
				if "skins" in rel_parts:
					idx = rel_parts.index("skins")
					if idx > 0:
						char_name = rel_parts[idx - 1]
				else:
					idx = rel_parts.index("characters")
					if idx < len(rel_parts) - 1:
						char_name = rel_parts[idx + 1]

				if char_name in EVENT_CHARACTERS:
					if file_lower in {f"{char_name}.bin", "root.bin"} or re.fullmatch(r"skin\d+\.bin", file_lower):
						vitals.append(p)
					continue

				if char_name in all_target_chars:
					if char_name in {"turret", "turretrubble", "inhibitor", "nexus"}:
						if re.fullmatch(r"skin\d+\.bin", file_lower):
							vitals.append(p)
					elif char_name in MINION_CHARACTERS:
						if file_lower in {"skin0.bin", "skin1.bin"}:
							vitals.append(p)
					elif file_lower == "skin0.bin":
						vitals.append(p)
					continue

				if char_name in BANNER_CHARACTERS:
					if file_lower in {"skin0.bin", "root.bin"}:
						vitals.append(p)
					continue

				continue

			if p.as_posix().lower().endswith("maps/shipping/map11/map11.bin") or file_lower == "map11.bin":
				vitals.append(p)
				continue

	return sorted(set(vitals))


def _iter_champion_wads(champions_dir: Path = DEFAULT_CHAMPIONS_DIR) -> list[Path]:
	champions_dir = Path(champions_dir)
	if not champions_dir.is_dir():
		return []
	filtered: list[Path] = []
	for path in champions_dir.iterdir():
		if not path.is_file() or not CHAMPION_WAD_PATTERN.fullmatch(path.name):
			continue
		lower_name = path.name.lower()
		if any(tag in lower_name for tag in ("tft", "jade_")) or re.search(r"\.[a-z]{2}[_-][a-z]{2}\.", lower_name):
			continue
		filtered.append(path)
	return sorted(filtered)


def find_files_by_extensions(directory: Path, extensions: set[str] | list[str] | str) -> list[Path]:
	if isinstance(extensions, str):
		extensions = {extensions}
	extensions_lower = {ext.lower() if ext.startswith('.') else '.' + ext.lower() for ext in extensions}
	matched: list[Path] = []
	for root, _, files in os.walk(str(directory)):
		for file_name in files:
			p = Path(root) / file_name
			if p.suffix.lower() in extensions_lower:
				matched.append(p)
	return matched


def _normalize_asset_path(path: str | Path) -> str:
	return str(path).replace("\\", "/").lstrip("/").lower()


def _is_mod_target_file(wad_name: str, rel_path: str, options: dict[str, bool] | None = None) -> bool:
	if options is None:
		options = {
			"accents": True,
			"wards": True,
			"structures": True,
			"minions": True,
			"finishers": True,
			"recalls": True,
			"champions": True,
		}
	opt_accents = options.get("accents", True)
	opt_wards = options.get("wards", True)
	opt_structures = options.get("structures", True)
	opt_minions = options.get("minions", True)
	opt_finishers = options.get("finishers", True)
	opt_recalls = options.get("recalls", True)
	opt_champions = options.get("champions", True)

	rel_lower = rel_path.lower()
	wad_lower = wad_name.lower()

	if opt_accents and rel_lower.endswith("transparent.tex"):
		return True

	if opt_finishers:
		if wad_lower in {"ui.wad.client", "global.wad.client"}:
			if any(k in rel_lower for k in ("nexusfinisher", "endofgame")):
				return True

	if opt_recalls and wad_lower == "global.wad.client" and "shared" in rel_lower:
		return True

	if opt_accents and wad_lower == "global.wad.client":
		if any(k in rel_lower for k in ("summonerbanner", "summonertroph")):
			return True

	if wad_lower.endswith(".wad.client") and wad_lower not in {"map11.wad.client", "global.wad.client", "ui.wad.client"}:
		if not opt_champions:
			return False
		if rel_lower.endswith(".skn"):
			return True
		m = re.search(r'[/\\]skins[/\\]skin(\d+)\.bin$', rel_lower)
		return bool(m and m.group(1) != "0")

	if wad_lower == "map11.wad.client":
		if opt_accents and any(ev in rel_lower for ev in EVENT_CHARACTERS):
			return True

		if "characters" in rel_lower:
			m = re.search(r'characters[/\\]([^/\\]+)(?:[/\\]skins)?[/\\]([^/\\]+)$', rel_lower)
			if m:
				char_name, file_name = m.group(1), m.group(2)

				if opt_accents and char_name in BANNER_CHARACTERS:
					return file_name in {"skin0.bin", "root.bin"}

				m_skin = re.fullmatch(r'skin(\d+)\.bin', file_name)
				if not m_skin:
					return False
				skin_id = m_skin.group(1)

				if char_name in {"turret", "inhibitor", "nexus"}:
					return opt_structures and skin_id not in {"0", "1", "2", "3"}

				if char_name == "turretrubble":
					return opt_structures and skin_id not in {"0", "1"}

				if skin_id in {"0", "1"} and char_name in MINION_CHARACTERS:
					return False

				if skin_id == "0":
					return False

				if opt_wards and char_name in WARD_CHARACTERS:
					return True
				if opt_structures and char_name in STRUCTURE_CHARACTERS:
					return True
				if opt_minions and (char_name in MINION_CHARACTERS or (char_name.startswith("sru_") and "minion" in char_name)):
					return True
				if opt_champions and char_name in MAP_CHAMPION_CHARACTERS:
					return True
			return False

		if "base_srx.materials.bin" in rel_lower:
			return opt_accents

		if rel_lower.endswith("maps/shipping/map11/map11.bin") or rel_lower.endswith("/map11.bin"):
			return opt_accents

	return False


def _clean_code_line(line: str) -> str:
	if "#" in line:
		line = line.split("#", 1)[0]
	if '"' in line:
		return re.sub(r'"(?:\\.|[^"\\])*"', '', line)
	return line


# ==============================================================================
# DETERMINISTIC MAP SANITIZATION (PRESERVING NATIVE TEXTURE PATHS)
# ==============================================================================
def _extract_diffuse_texture(block_text: str) -> str:
	m = re.search(r'(?i)\btexturePath:\s*file\s*=\s*"([^"]+)"', block_text)
	if not m:
		raise ValueError(f"Corrupted or invalid StaticMaterialDef block: missing texturePath!\n{block_text[:200]}")
	return m.group(1)


def _make_neutralized_material(key: str, block_text: str, native_texture_path: str) -> str:
	m_name = re.search(r'(?i)^\s*(name:\s*hash\[u64\]\s*=\s*0x[0-9a-fA-F]+)', block_text, re.MULTILINE)
	if not m_name:
		m_name = re.search(r'(?i)^\s*(name:\s*[^\r\n]+)', block_text, re.MULTILINE)
	name_line = m_name.group(1).strip() if m_name else 'name: string = "Neutralized"'

	return (
		f'    "{key}" = StaticMaterialDef {{\n'
		f'        {name_line}\n'
		f'        type: u8 = 0\n'
		f'        samplerValues: list2[embed] = {{\n'
		f'            StaticMaterialShaderSamplerDef {{\n'
		f'                TextureName: string = "DiffuseTexture"\n'
		f'                texturePath: file = "{native_texture_path}"\n'
		f'                addressW: u32 = 1\n'
		f'            }}\n'
		f'        }}\n'
		f'        paramValues: list2[embed] = {{\n'
		f'            StaticMaterialShaderParamDef {{\n'
		f'                name: string = "AlphaTestValue"\n'
		f'                value: vec4 = {{ 2, 0, 0, 0 }}\n'
		f'            }}\n'
		f'            StaticMaterialShaderParamDef {{\n'
		f'                name: string = "TintColor"\n'
		f'                value: vec4 = {{ 0, 0, 0, 0 }}\n'
		f'            }}\n'
		f'        }}\n'
		f'        techniques: list[embed] = {{\n'
		f'            StaticMaterialTechniqueDef {{\n'
		f'                name: string = "normal"\n'
		f'                passes: list[embed] = {{\n'
		f'                    StaticMaterialPassDef {{\n'
		f'                        shader: link = "Shaders/StaticMesh/DefaultEnv_Flat_AlphaTest"\n'
		f'                        blendEnable: bool = true\n'
		f'                        dstColorBlendFactor: u32 = 7\n'
		f'                        dstAlphaBlendFactor: u32 = 7\n'
		f'                    }}\n'
		f'                }}\n'
		f'            }}\n'
		f'        }}\n'
		f'    }}\n'
	)


def _make_empty_vfx(key: str) -> str:
	if key:
		return (
			f'    "{key}" = VfxSystemDefinitionData {{\n'
			f'        complexEmitterDefinitionData: list[pointer] = {{}}\n'
			f'        simpleEmitterDefinitionData: list[pointer] = {{}}\n'
			f'    }}\n'
		)
	return (
		f'    VfxSystemDefinitionData {{\n'
		f'        complexEmitterDefinitionData: list[pointer] = {{}}\n'
		f'        simpleEmitterDefinitionData: list[pointer] = {{}}\n'
		f'    }}\n'
	)


def _sanitize_event_npc_text(text: str) -> tuple[str, int]:
	changes = 0
	out_text = text

	out_text, c_tex = re.subn(r'(?i)(\btexture:\s*file\s*=\s*)"[^"]+"', rf'\1"{MAP11_TRANSPARENT_TEX_REL}"', out_text)
	out_text, c_glow = re.subn(r'(?i)(\bselfIllumination:\s*f32\s*=\s*)[0-9.]+', r'\g<1>0.0', out_text)
	out_text, c_rate = re.subn(r'(?i)(\brate:\s*embed\s*=\s*ValueFloat\s*\{[^}]*?constantValue:\s*f32\s*=\s*)[0-9.]+', r'\g<1>0', out_text)
	out_text, c_life = re.subn(r'(?i)(\bparticleLifetime:\s*embed\s*=\s*ValueFloat\s*\{[^}]*?constantValue:\s*f32\s*=\s*)[0-9.]+', r'\g<1>0', out_text)
	out_text, c_linger = re.subn(r'(?i)(\bparticleLinger:\s*option\[f32\]\s*=\s*\{\s*)[0-9.]+', r'\g<1>0', out_text)
	out_text, c_scale = re.subn(r'(?i)(\bbirthScale0:\s*embed\s*=\s*ValueVector3\s*\{[^}]*?constantValue:\s*vec3\s*=\s*)\{[^}]*\}', r'\g<1>{ 0, 0, 0 }', out_text)
	out_text, c_hp = re.subn(r'(?i)(\bhealthBarHeight:\s*f32\s*=\s*)[0-9.]+', r'\g<1>0', out_text)
	out_text, c_untarg = re.subn(r'(?i)(\bshowWhileUntargetable:\s*bool\s*=\s*)true', r'\g<1>false', out_text)
	out_text, c_snd = re.subn(r'(?i)(\bsoundPersistentDefault:\s*string\s*=\s*)"[^"]*"', r'\1""', out_text)

	changes += (c_tex + c_glow + c_rate + c_life + c_linger + c_scale + c_hp + c_untarg + c_snd)
	return out_text, changes


def _sanitize_text_content(text: str, filename: str) -> tuple[str, int]:
	changes = 0
	out_text = text
	fn_lower = filename.replace("\\", "/").lower()

	if "map11.bin" in fn_lower:
		out_text, c_geo = re.subn(
			r'(?i)(\bmMapContainerLink\s*:\s*\w+\s*=\s*)"[^"]+"',
			r'\g<1>"Maps/MapGeometry/Map11/Base_SRX"',
			out_text
		)
		out_text, c_cfg = re.subn(
			r'(?i)(\bmMapObjectsCFG\s*:\s*\w+\s*=\s*)"[^"]*"',
			r'\g<1>"ASSETS/Maps/Deprecated/Map11/CFG/objectcfg_SRX.cfg"',
			out_text
		)
		out_text, c_ini = re.subn(
			r'(?i)(\bmWorldParticlesINI\s*:\s*\w+\s*=\s*)"[^"]*"',
			r'\g<1>"ASSETS/Maps/Particles/Deprecated/Map11/Particles_SRX.ini"',
			out_text
		)
		out_text, c_res = re.subn(
			r'(?i)(\bmResourceResolvers\s*:\s*list\[link\]\s*=\s*\{)[^}]*\}',
			r'\g<1> "Maps/Shipping/Map11/MapSkins/Default/Resources" }',
			out_text
		)
		changes += (c_geo + c_cfg + c_ini + c_res)

	for pattern, replacer in _SRS_TO_CANONICAL_TEX_RULES:
		out_text, c_sub = pattern.subn(replacer, out_text)
		changes += c_sub

	lines = out_text.splitlines(keepends=True)
	out_lines: list[str] = []
	i = 0

	while i < len(lines):
		line = lines[i]
		if "{" not in line:
			out_lines.append(line)
			i += 1
			continue

		m = re.match(r'^\s*(?:"([^"]+)"|([^\s=]+))\s*=\s*([A-Za-z0-9_]+)\s*\{', line)
		if not m:
			out_lines.append(line)
			i += 1
			continue

		key = m.group(1) or m.group(2) or ""
		entry_type = m.group(3)

		clean_line = _clean_code_line(line)
		depth = clean_line.count("{") - clean_line.count("}")
		block_lines = [line]
		i += 1
		while i < len(lines) and depth > 0:
			next_line = lines[i]
			block_lines.append(next_line)
			if "{" in next_line or "}" in next_line:
				clean_next = _clean_code_line(next_line)
				depth += clean_next.count("{") - clean_next.count("}")
			i += 1

		type_lower = entry_type.lower()
		if type_lower not in _TARGET_TYPES:
			out_lines.extend(block_lines)
			continue

		block_text = "".join(block_lines)
		is_addon = any(p.search(key) for p in _ACCENT_OMISSION_PATTERNS) or any(p.search(block_text) for p in _ACCENT_OMISSION_PATTERNS)

		if is_addon:
			if type_lower == "staticmaterialdef":
				native_tex = _extract_diffuse_texture(block_text)
				out_lines.append(_make_neutralized_material(key, block_text, native_tex))
			else:
				out_lines.append(_make_empty_vfx(key))
			changes += 1
			continue

		out_lines.append(block_text)

	out_text = "".join(out_lines)

	if any(ev in fn_lower for ev in EVENT_CHARACTERS):
		out_text, c_ev = _sanitize_event_npc_text(out_text)
		changes += c_ev

	return out_text, changes


def clean_map_elements(
	wad_dir: Path,
	dry_run: bool = False,
	progress_callback: Callable[[int, int, str], None] | None = None,
) -> list[Path]:
	modified_files: list[Path] = []
	all_txt = find_files_by_extensions(wad_dir, ".txt")
	target_files: list[Path] = []
	for f in all_txt:
		rel_posix = f.relative_to(wad_dir).as_posix().lower()
		if "characters" in rel_posix and not any(ev in rel_posix for ev in EVENT_CHARACTERS):
			continue
		target_files.append(f)

	total = len(target_files)
	if total == 0:
		return modified_files

	for index, txt_file in enumerate(target_files, start=1):
		if progress_callback:
			progress_callback(index, total, txt_file.name)

		rel_posix = txt_file.relative_to(wad_dir).as_posix().lower()
		try:
			text = txt_file.read_text(encoding="utf-8", errors="ignore")
		except Exception:
			continue

		cleaned, n_changes = _sanitize_text_content(text, rel_posix)
		if cleaned != text and n_changes > 0:
			if not dry_run:
				txt_file.parent.mkdir(parents=True, exist_ok=True)
				txt_file.write_text(cleaned, encoding="utf-8")
			modified_files.append(txt_file)
			LOG.info("Sanitized (%d changes): %s", n_changes, rel_posix)

	return modified_files


# ==============================================================================
# FINISHER SANITIZATION (DEPTH-AWARE & PR #284 SCHEMA COMPLIANT)
# ==============================================================================
def _sanitize_finisher_text(text: str) -> tuple[str, int]:
	changes = 0
	out_text = text

	out_text, c1 = re.subn(
		r'(?i)(UICrepe(?:Chaos|Order)["\']\s*=\s*UiPropertyOverrideLoadable\s*\{[\s\S]*?FilepathHash:\s*(?:file|hash(?:\[[^\]]+\])?)\s*=\s*)"[^"]+"',
		r'\1"clientstates/gameplay/ux/endofgame/uibase"',
		out_text
	)
	out_text, c2 = re.subn(
		r'(?i)(UICrepe(?:Chaos|Order)["\']\s*=\s*UiPropertyOverrideLoadable\s*\{[\s\S]*?OverrideSrcFolder:\s*(?:file|hash(?:\[[^\]]+\])?)\s*=\s*)"[^"]+"',
		r'\1"clientstates/gameplay/ux/endofgame/uibase"',
		out_text
	)

	out_text, c3 = re.subn(
		r'(?i)AudioBankPaths\s*:\s*list2\[string\]\s*=\s*\{[\s\S]*?\}',
		'AudioBankPaths: list2[string] = {}',
		out_text
	)

	out_text, c_bool = re.subn(
		r'(?i)(\b(?:0x[0-9a-fA-F]+|(?:m_?)?(?:is)?(?:enabled|active))\s*:\s*bool\s*=\s*)true',
		r'\g<1>false',
		out_text
	)
	out_text, c_dis = re.subn(r'(?i)(\bdisabled\s*:\s*bool\s*=\s*)false', r'\g<1>true', out_text)

	out_text, c_seq = re.subn(r'(?i)(\w*Sequence\s*:\s*hash\s*=\s*)"[^"]+"', r'\g<1>0x00000000', out_text)
	out_text, c_ui = re.subn(r'(?i)(Ui(?:Loadable|ElementGroup)\s*:\s*hash\s*=\s*)"[^"]+"', r'\g<1>0x00000000', out_text)

	changes += (c1 + c2 + c3 + c_bool + c_dis + c_seq + c_ui)

	lines = out_text.splitlines(keepends=True)
	out_lines: list[str] = []
	i = 0

	while i < len(lines):
		line = lines[i]
		clean_l = _clean_code_line(line)
		
		if re.search(r'(?i)(?:complexEmitterDefinitionData|simpleEmitterDefinitionData)\s*:\s*list\[pointer\]\s*=\s*\{', clean_l):
			field_name = "complexEmitterDefinitionData" if "complex" in clean_l.lower() else "simpleEmitterDefinitionData"
			depth = clean_l.count("{") - clean_l.count("}")
			i += 1
			while i < len(lines) and depth > 0:
				next_l = _clean_code_line(lines[i])
				depth += next_l.count("{") - next_l.count("}")
				i += 1
			out_lines.append(f"\t\t{field_name}: list[pointer] = {{}}\n")
			changes += 1
			continue

		out_lines.append(line)
		i += 1

	return "".join(out_lines), changes


def clean_finisher_elements(
	wad_dir: Path,
	dry_run: bool = False,
	progress_callback: Callable[[int, int, str], None] | None = None,
) -> list[Path]:
	modified_files: list[Path] = []
	txt_files = find_files_by_extensions(wad_dir, ".txt")
	
	target_files: list[Path] = []
	for p in txt_files:
		rel = p.relative_to(wad_dir).as_posix().lower()
		if any(k in rel for k in ("nexusfinisher", "endofgame")):
			target_files.append(p)
			
	total = len(target_files)
	for index, txt_file in enumerate(target_files, start=1):
		rel_posix = txt_file.relative_to(wad_dir).as_posix().lower()
		if progress_callback:
			progress_callback(index, total, txt_file.name)

		try:
			text = txt_file.read_text(encoding="utf-8", errors="ignore")
		except Exception:
			continue

		cleaned, n_changes = _sanitize_finisher_text(text)
		if cleaned != text and n_changes > 0:
			if not dry_run:
				txt_file.parent.mkdir(parents=True, exist_ok=True)
				txt_file.write_text(cleaned, encoding="utf-8")
			modified_files.append(txt_file)
			LOG.info("Sanitized Master Finisher (%d changes): %s", n_changes, rel_posix)

	return modified_files

def _sanitize_banner_trophy(text: str) -> tuple[str, int]:
	changes = 0
	out_text = text

	out_text, c_tex = re.subn(r'(?i)(\btexture:\s*file\s*=\s*)"[^"]+"', rf'\1"{GLOBAL_TRANSPARENT_TEX_REL}"', out_text)
	out_text, c_glow = re.subn(r'(?i)(\bselfIllumination:\s*f32\s*=\s*)[0-9.]+', r'\g<1>0.0', out_text)
	changes += (c_tex + c_glow)

	lines = out_text.splitlines(keepends=True)
	out_lines: list[str] = []
	i = 0

	while i < len(lines):
		line = lines[i]
		if "{" not in line:
			out_lines.append(line)
			i += 1
			continue

		m = re.match(r'^\s*(?:"([^"]+)"|([^\s=]+))\s*=\s*([A-Za-z0-9_]+)\s*\{', line)
		if not m:
			out_lines.append(line)
			i += 1
			continue

		key = m.group(1) or m.group(2) or ""
		entry_type = m.group(3)

		clean_line = _clean_code_line(line)
		depth = clean_line.count("{") - clean_line.count("}")
		block_lines = [line]
		i += 1
		while i < len(lines) and depth > 0:
			next_line = lines[i]
			block_lines.append(next_line)
			if "{" in next_line or "}" in next_line:
				clean_next = _clean_code_line(next_line)
				depth += clean_next.count("{") - clean_next.count("}")
			i += 1

		if entry_type.lower() == "vfxsystemdefinitiondata":
			out_lines.append(_make_empty_vfx(key))
			changes += 1
			continue

		out_lines.append("".join(block_lines))

	return "".join(out_lines), changes


def _sanitize_arena_banner_text(text: str) -> tuple[str, int]:
	changes = 0
	out_text = text
	out_text, c_tex = re.subn(r'(?i)(\btexture:\s*file\s*=\s*)"[^"]+"', rf'\1"{MAP11_TRANSPARENT_TEX_REL}"', out_text)
	out_text, c_glow = re.subn(r'(?i)(\bselfIllumination:\s*f32\s*=\s*)[0-9.]+', r'\g<1>0.0', out_text)
	changes += (c_tex + c_glow)
	return out_text, changes


def clean_summoner_elements(
	wad_dir: Path,
	dry_run: bool = False,
	progress_callback: Callable[[int, int, str], None] | None = None,
) -> list[Path]:
	modified_files: list[Path] = []
	txt_files = find_files_by_extensions(wad_dir, ".txt")
	target_files = [p for p in txt_files if any(k in p.name.lower() for k in ("summonerbanner", "summonertroph"))]
	total = len(target_files)

	for index, txt_file in enumerate(target_files, start=1):
		rel_posix = txt_file.relative_to(wad_dir).as_posix().lower()
		if progress_callback:
			progress_callback(index, total, txt_file.name)

		try:
			text = txt_file.read_text(encoding="utf-8", errors="ignore")
		except Exception:
			continue

		cleaned, n_changes = _sanitize_banner_trophy(text)
		if cleaned != text and n_changes > 0:
			if not dry_run:
				txt_file.parent.mkdir(parents=True, exist_ok=True)
				txt_file.write_text(cleaned, encoding="utf-8")
			modified_files.append(txt_file)
			LOG.info("Sanitized Summoner Banner/Trophy (%d changes): %s", n_changes, rel_posix)

	return modified_files


# ==============================================================================
# SPECIAL RECALL SANITIZATION (GLOBAL.WAD.CLIENT -> SHARED)
# ==============================================================================
def resolve_recall_template(workspace_root: Path, template_stem: str) -> Path | None:
	overrides_dir = Path(workspace_root) / "overrides"
	candidates = [
		overrides_dir / "global" / "shared" / f"{template_stem}.bin.txt",
		overrides_dir / "global" / "shared" / f"{template_stem}.txt",
	]
	for candidate in candidates:
		if candidate.is_file():
			return candidate
	return None


def _apply_recall_template(workspace_root: Path, key: str, fallback_stem: str) -> str | None:
	clean_key = key.replace('"', '').strip()
	short_name = clean_key.split("/")[-1]
	tpl_path = resolve_recall_template(workspace_root, short_name.lower()) or resolve_recall_template(workspace_root, fallback_stem)
	if not tpl_path:
		return None
	try:
		text = tpl_path.read_text(encoding="utf-8", errors="ignore")
		text = text.replace("{RECALL_KEY}", clean_key).replace("{RECALL_NAME}", short_name)
		text = re.sub(r'(?i)^\s*(?:"[^"]+"|[^\s=]+)\s*=\s*VfxSystemDefinitionData', f'    "{clean_key}" = VfxSystemDefinitionData', text, count=1)
		text = re.sub(r'(?i)(particleName:\s*string\s*=\s*)"[^"]*"', rf'\1"{short_name}"', text)
		text = re.sub(r'(?i)(particlePath:\s*string\s*=\s*)"[^"]*"', rf'\1"{clean_key}"', text)
		text = re.sub(r'(?i)(objectPath:\s*hash(?:\[[^\]]+\])?\s*=\s*)"[^"]*"', rf'\1"{clean_key}"', text)
		return text if text.endswith("\n") else text + "\n"
	except OSError:
		return None


def _sanitize_recall_text(text: str, workspace_root: Path = DEFAULT_WORKSPACE_ROOT) -> tuple[str, int]:
	changes = 0
	lines = text.splitlines(keepends=True)
	out_lines: list[str] = []
	i = 0

	standard_home_body: str | None = None
	for j, line in enumerate(lines):
		m = re.match(r'^\s*(?:"([^"]+)"|([^\s=]+))\s*=\s*VfxSystemDefinitionData\s*\{', line)
		if m:
			k = (m.group(1) or m.group(2) or "").lower()
			if k.endswith("teleporthome") and not any(x in k for x in ("honor", "challenger", "arrive")):
				clean_l = _clean_code_line(line)
				depth = clean_l.count("{") - clean_l.count("}")
				block_inner = []
				curr = j + 1
				while curr < len(lines) and depth > 0:
					nxt = lines[curr]
					if "{" in nxt or "}" in nxt:
						c_nxt = _clean_code_line(nxt)
						depth += c_nxt.count("{") - c_nxt.count("}")
					if depth > 0:
						block_inner.append(nxt)
					curr += 1
				standard_home_body = "".join(block_inner)
				break

	while i < len(lines):
		line = lines[i]
		if "{" not in line:
			out_lines.append(line)
			i += 1
			continue

		m = re.match(r'^\s*(?:"([^"]+)"|([^\s=]+))\s*=\s*([A-Za-z0-9_]+)\s*\{', line)
		if not m:
			out_lines.append(line)
			i += 1
			continue

		key = m.group(1) or m.group(2) or ""
		entry_type = m.group(3)

		clean_line = _clean_code_line(line)
		depth = clean_line.count("{") - clean_line.count("}")
		block_lines = [line]
		i += 1
		while i < len(lines) and depth > 0:
			next_line = lines[i]
			block_lines.append(next_line)
			if "{" in next_line or "}" in next_line:
				clean_next = _clean_code_line(next_line)
				depth += clean_next.count("{") - clean_next.count("}")
			i += 1

		if entry_type.lower() == "vfxsystemdefinitiondata" and _RECALL_VFX_PATTERN.search(key):
			key_lower = key.lower()
			if "arrive" in key_lower:
				arrive_block = _apply_recall_template(workspace_root, key, "arrive")
				if arrive_block:
					out_lines.append(arrive_block)
					changes += 1
				else:
					out_lines.extend(block_lines)
			else:
				home_block = _apply_recall_template(workspace_root, key, "home")
				if home_block:
					out_lines.append(home_block)
					changes += 1
				elif standard_home_body:
					short_name = key.split("/")[-1].replace('"', '')
					cloned = re.sub(r'(?i)(particleName:\s*string\s*=\s*)"[^"]*"', rf'\1"{short_name}"', standard_home_body)
					cloned = re.sub(r'(?i)(particlePath:\s*string\s*=\s*)"[^"]*"', rf'\1"{key}"', cloned)
					cloned = re.sub(r'(?i)(objectPath:\s*hash(?:\[[^\]]+\])?\s*=\s*)"[^"]*"', rf'\1"{key}"', cloned)
					out_lines.append(f'    "{key}" = VfxSystemDefinitionData {{\n{cloned}    }}\n')
					changes += 1
				else:
					out_lines.extend(block_lines)
		else:
			out_lines.extend(block_lines)

	return "".join(out_lines), changes


def clean_recall_elements(
	wad_dir: Path,
	workspace_root: Path = DEFAULT_WORKSPACE_ROOT,
	dry_run: bool = False,
	progress_callback: Callable[[int, int, str], None] | None = None,
) -> list[Path]:
	modified_files: list[Path] = []
	txt_files = find_files_by_extensions(wad_dir, ".txt")
	target_files = [p for p in txt_files if any(k in p.name.lower() for k in ("shared", "troy", "recall"))]
	total = len(target_files)

	for index, txt_file in enumerate(target_files, start=1):
		rel_posix = txt_file.relative_to(wad_dir).as_posix().lower()
		if progress_callback:
			progress_callback(index, total, txt_file.name)

		try:
			text = txt_file.read_text(encoding="utf-8", errors="ignore")
		except Exception:
			continue

		cleaned, n_changes = _sanitize_recall_text(text, workspace_root=workspace_root)
		if cleaned != text and n_changes > 0:
			if not dry_run:
				txt_file.parent.mkdir(parents=True, exist_ok=True)
				txt_file.write_text(cleaned, encoding="utf-8")
			modified_files.append(txt_file)
			LOG.info("Sanitized Special Recall Overlays (%d changes): %s", n_changes, rel_posix)

	return modified_files


# ==============================================================================
# CHAMPION SPECIFICS HANDLER & SUBMESH ALIAS ENGINE
# ==============================================================================
def resolve_champion_template(workspace_root: Path, entity: str, skin_stem: str) -> Path | None:
	overrides_dir = Path(workspace_root) / "overrides"
	candidates = [
		overrides_dir / entity / "skins" / f"{skin_stem}.bin.txt",
		overrides_dir / "data" / "characters" / entity / "skins" / f"{skin_stem}.bin.txt",
		overrides_dir / entity / f"{skin_stem}.bin.txt",
	]
	for candidate in candidates:
		if candidate.is_file() and candidate.stat().st_size > 0:
			return candidate
	return None


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


def force_vanilla_existing_skins(
	wad_dir: Path,
	workspace_root: Path,
	main_entity: str = "",
	dry_run: bool = False,
	progress_callback: Callable[[int, int, str], None] | None = None,
) -> tuple[list[Path], list[tuple[str, str, str, str]]]:
	target_bins = _iter_target_champion_files(wad_dir)
	total = len(target_bins)
	generated_files: list[Path] = []
	raw_base_cache: dict[Path, str] = {}
	chroma_inheritance_map: dict[str, Path] = {}
	submesh_alias_tasks: list[tuple[str, str, str, str]] = []

	overrides_dir = Path(workspace_root) / "overrides"
	char_name = main_entity.lower()
	champ_tpl_dir = overrides_dir / char_name / "skins"
	if champ_tpl_dir.is_dir():
		for tpl_file in champ_tpl_dir.glob("skin*.bin.txt"):
			if tpl_file.stat().st_size == 0:
				continue
			try:
				header = tpl_file.read_text(encoding="utf-8", errors="ignore")[:400]
				m_chromas = re.search(r'(?i)#\s*VARIANTS\s*:\s*([^\r\n]+)', header)
				if m_chromas:
					for raw_token in m_chromas.group(1).split(","):
						token = raw_token.strip().lower()
						if "-" in token:
							m_range = re.match(r'(\d+)\s*-\s*(\d+)', token)
							if m_range:
								for cid in range(int(m_range.group(1)), int(m_range.group(2)) + 1):
									chroma_inheritance_map[str(cid)] = tpl_file
						else:
							m_single = re.search(r'(\d+)', token)
							if m_single:
								chroma_inheritance_map[m_single.group(1)] = tpl_file
			except OSError:
				pass

	for index, target_bin in enumerate(target_bins, start=1):
		match = re.fullmatch(r"skin(?:-1|(\d+)|(-[a-z0-9]+))", target_bin.stem, re.IGNORECASE)
		if not match:
			continue
		suffix = match.group(1) or match.group(2) or "-1"
		target_skin = f"Skin{suffix}"
		skin_numeric_id = match.group(1)

		c_name = target_bin.parent.parent.name.lower()
		out_txt = target_bin.with_suffix(target_bin.suffix + ".txt")

		template_file = resolve_champion_template(workspace_root, c_name, target_bin.stem.lower())
		
		if not template_file and skin_numeric_id and skin_numeric_id in chroma_inheritance_map:
			template_file = chroma_inheritance_map[skin_numeric_id]
			LOG.info("Auto-inheriting template for %s/%s from parent root %s", c_name, target_skin, template_file.name)

		if template_file:
			try:
				raw_template = template_file.read_text(encoding="utf-8", errors="ignore")
				m_alias = re.search(r'(?i)#\s*SUBMESH_ALIAS\s*:\s*(\S+)\s*->\s*([^\r\n]+)', raw_template)

				if m_alias:
					m_parent_skin = re.search(r'skin(\d+)', template_file.stem, re.IGNORECASE)
					parent_skin_sfx = m_parent_skin.group(1) if m_parent_skin else suffix
					alias_tuple = (c_name, parent_skin_sfx, m_alias.group(1).strip(), m_alias.group(2).strip())
					if not any(t[0] == c_name and t[1] == alias_tuple[1] and t[2] == alias_tuple[2] and t[3] == alias_tuple[3] for t in submesh_alias_tasks):
						submesh_alias_tasks.append(alias_tuple)

				clean_lines = [
					l for l in raw_template.splitlines()
					if not (l.strip().startswith("#") and not l.strip().startswith("#PROP"))
				]
				template_content = "\n".join(clean_lines)

				if c_name == main_entity:
					base_f = target_bin.parent / "skin0.bin.txt"
					if base_f.is_file():
						try:
							live_base_text = base_f.read_text(encoding="utf-8", errors="ignore")
							template_content = sync_template_linked_block(template_content, live_base_text)
						except OSError:
							pass

				if skin_numeric_id and target_bin.stem.lower() not in template_file.name.lower():
					template_content = fast_vanilla_revert(
						template_content,
						target_skin,
						skin_numeric_id,
					)
				if not dry_run:
					out_txt.parent.mkdir(parents=True, exist_ok=True)
					out_txt.write_text(template_content, encoding="utf-8")
				generated_files.append(out_txt)
				LOG.info("Applied curated override for %s/%s", c_name, target_bin.name)
				if progress_callback:
					progress_callback(index, total, target_bin.name)
				continue
			except OSError as exc:
				LOG.warning("Failed to read override %s: %s", template_file, exc)

		base_file = target_bin.parent / "skin0.bin.txt"
		if not base_file.is_file():
			candidates = list(target_bin.parent.glob("[sS][kK][iI][nN]0.bin.txt"))
			if candidates:
				base_file = candidates[0]
			else:
				continue

		if base_file not in raw_base_cache:
			try:
				raw_base_cache[base_file] = base_file.read_text(encoding="utf-8", errors="ignore")
			except OSError:
				raw_base_cache[base_file] = ""

		base_text = raw_base_cache[base_file]
		if not base_text:
			continue

		text = fast_vanilla_revert(
			base_text,
			target_skin,
			skin_numeric_id,
		)

		if not dry_run:
			out_txt.parent.mkdir(parents=True, exist_ok=True)
			out_txt.write_text(text, encoding="utf-8")
		generated_files.append(out_txt)

		if progress_callback:
			progress_callback(index, total, target_bin.name)

	return generated_files, submesh_alias_tasks


def _expand_ward_skin0_only_hash(char_name: str, base_text: str, target_skin: str) -> str:
	text = base_text
	text = re.sub(rf'(?i)("(?:Characters[/\\])?{re.escape(char_name)}[/\\]Skins[/\\])Skin0"', rf'\g<1>{target_skin}"', text)
	text = re.sub(rf'(?i)(objectPath:\s*hash(?:\[[^\]]+\])?\s*=\s*"(?:Characters[/\\])?{re.escape(char_name)}[/\\]Skins[/\\])Skin0"', rf'\g<1>{target_skin}"', text)
	text = re.sub(rf'(?i)("(?:Characters[/\\])?{re.escape(char_name)}[/\\]Skins[/\\])Skin0([/\\]Resources")', rf'\g<1>{target_skin}\2', text)
	text = re.sub(rf'(?i)(link\s*=\s*"(?:Characters[/\\])?{re.escape(char_name)}[/\\]Skins[/\\])Skin0([/\\]Resources")', rf'\g<1>{target_skin}\2', text)
	return text


_OTHER_TAGS = "|".join(sorted(_OTHER_INTEGRITY, key=len, reverse=True))
_OTHER_INTEGRITY_PATTERN = re.compile(rf'(?i)(?<![a-z0-9])(?:{_OTHER_TAGS})(?![a-z0-9])')
_OTHER_INTEGRITY_PATHS = re.compile(rf'(?i)[/\\](?:{_OTHER_TAGS})[/\\]')

def is_other_mode_structure(skin_name: str, content: str = "") -> bool:
	if skin_name and _OTHER_INTEGRITY_PATTERN.search(skin_name):
		return True
	if content and _OTHER_INTEGRITY_PATHS.search(content):
		return True
	return False


# ==============================================================================
# LEGACY MAP11 MASTER CHARACTERS ENGINE (TURRETS, RUBBLE, MINIONS, WARDS, BANNERS)
# ==============================================================================
def process_map11_master_characters(
	map11_wad_dir: Path,
	dry_run: bool = False,
	progress_callback: Callable[[int, int, str], None] | None = None,
) -> list[Path]:
	char_skins_dirs: dict[str, Path] = {}
	candidate_roots = [
		map11_wad_dir / "data" / "characters",
		map11_wad_dir / "assets" / "characters",
	]
	search_roots = [p for p in candidate_roots if p.is_dir()] or [map11_wad_dir]
	for s_root in search_roots:
		for current, _, _ in os.walk(str(s_root)):
			c_path = Path(current)
			parts = [p.lower() for p in c_path.parts]
			if len(parts) >= 2 and parts[-1] == "skins":
				char_name = parts[-2]
				char_skins_dirs[char_name] = c_path
			elif len(parts) >= 2 and parts[-2] == "characters":
				char_name = parts[-1]
				if (c_path / "skin0.bin").is_file() and char_name not in char_skins_dirs:
					char_skins_dirs[char_name] = c_path

	generated_files: list[Path] = []
	standard_tasks: list[tuple[str, Path, str]] = []
	turret_tasks: list[tuple[str, Path, str, str]] = []
	rubble_tasks: list[tuple[str, Path, str, str]] = []
	minion_tasks: list[tuple[str, Path, str, str]] = []

	all_standard_chars = STRUCTURE_CHARACTERS | MINION_CHARACTERS | MAP_CHAMPION_CHARACTERS

	for char_name in sorted(all_standard_chars):
		skins_dir = char_skins_dirs.get(char_name)
		if not skins_dir or not skins_dir.is_dir():
			continue

		if char_name in {"turret", "inhibitor", "nexus"}:
			base_0 = skins_dir / "skin0.bin.txt"
			base_1 = skins_dir / "skin1.bin.txt"
			base_2 = skins_dir / "skin2.bin.txt"
			base_3 = skins_dir / "skin3.bin.txt"
			if all(b.is_file() for b in (base_0, base_1, base_2, base_3)):
				for p in sorted(skins_dir.glob("skin*.bin.txt")):
					m = re.fullmatch(r"skin(\d+)\.bin\.txt", p.name, re.IGNORECASE)
					if not m or m.group(1) in {"0", "1", "2", "3"}:
						continue
					skin_id = m.group(1)
					try:
						curr_txt = p.read_text(encoding="utf-8", errors="ignore")
					except OSError:
						curr_txt = ""

					m_name = re.search(r'(?i)\bchampionSkinName:\s*string\s*=\s*"([^"]+)"', curr_txt)
					s_name = m_name.group(1).lower() if m_name else ""

					if is_other_mode_structure(s_name, curr_txt):
						continue

					if "chaos" in s_name:
						is_chaos = True
					elif "order" in s_name:
						is_chaos = False
					else:
						is_chaos = ((int(skin_id) % 4) in {2, 3})

					if "red" in s_name:
						is_red = True
					elif "blue" in s_name:
						is_red = False
					else:
						is_red = (int(skin_id) % 2 == 1)

					if not is_chaos and not is_red:
						target_base_id = "0"
					elif not is_chaos and is_red:
						target_base_id = "1"
					elif is_chaos and not is_red:
						target_base_id = "2"
					else:
						target_base_id = "3"

					turret_tasks.append((char_name, skins_dir, skin_id, target_base_id))
			continue

		if char_name == "turretrubble":
			rubble_base_0 = skins_dir / "skin0.bin.txt"
			rubble_base_1 = skins_dir / "skin1.bin.txt"
			if rubble_base_0.is_file() and rubble_base_1.is_file():
				for p in sorted(skins_dir.glob("skin*.bin.txt")):
					m = re.fullmatch(r"skin(\d+)\.bin\.txt", p.name, re.IGNORECASE)
					if not m or m.group(1) in {"0", "1"}:
						continue
					skin_id = m.group(1)
					try:
						curr_txt = p.read_text(encoding="utf-8", errors="ignore")
					except OSError:
						curr_txt = ""

					m_name = re.search(r'(?i)\bchampionSkinName:\s*string\s*=\s*"([^"]+)"', curr_txt)
					s_name = m_name.group(1).lower() if m_name else ""

					if is_other_mode_structure(s_name, curr_txt):
						continue

					curr_lower = curr_txt.lower()
					is_chaos = ("chaos" in s_name or "chaos" in curr_lower or "red" in s_name or "red" in curr_lower or (int(skin_id) % 2 == 1))
					target_base = "1" if is_chaos else "0"
					rubble_tasks.append((char_name, skins_dir, skin_id, target_base))
			continue

		if char_name in MINION_CHARACTERS:
			minion_base_0 = skins_dir / "skin0.bin.txt"
			minion_base_1 = skins_dir / "skin1.bin.txt"
			if minion_base_0.is_file():
				for p in sorted(skins_dir.glob("skin*.bin")):
					m = re.fullmatch(r"skin(\d+)\.bin", p.name, re.IGNORECASE)
					if not m or m.group(1) in {"0", "1"}:
						continue
					skin_id = m.group(1)
					is_mirror = (int(skin_id) % 2 == 1)
					target_base = "1" if (is_mirror and minion_base_1.is_file()) else "0"
					minion_tasks.append((char_name, skins_dir, skin_id, target_base))
			continue

		base_file = skins_dir / "skin0.bin.txt"
		if not base_file.is_file():
			continue
		for p in sorted(skins_dir.glob("skin*.bin")):
			m = re.fullmatch(r"skin(\d+)\.bin", p.name, re.IGNORECASE)
			if m and m.group(1) != "0":
				standard_tasks.append((char_name, skins_dir, m.group(1)))

	sightward_skins_dir = char_skins_dirs.get("sightward")
	yellowtrinket_skins_dir = char_skins_dirs.get("yellowtrinket")
	jammerdevice_skins_dir = char_skins_dirs.get("jammerdevice")
	bluetrinket_skins_dir = char_skins_dirs.get("bluetrinket")

	valid_ward_skin_ids: list[str] = []
	if sightward_skins_dir and sightward_skins_dir.is_dir():
		for p in sorted(sightward_skins_dir.glob("skin*.bin")):
			m = re.fullmatch(r"skin(\d+)\.bin", p.name, re.IGNORECASE)
			if m and m.group(1) != "0":
				valid_ward_skin_ids.append(m.group(1))

	esports_banner_tasks: list[tuple[str, Path, str]] = []
	for b_name in sorted(BANNER_CHARACTERS):
		b_dir = char_skins_dirs.get(b_name)
		if not b_dir or not b_dir.is_dir():
			continue
		for tgt in ("skin0.bin.txt", "root.bin.txt"):
			tgt_file = b_dir / tgt
			if tgt_file.is_file():
				esports_banner_tasks.append((b_name, b_dir, tgt))

	total_operations = (
		len(standard_tasks)
		+ len(turret_tasks)
		+ len(rubble_tasks)
		+ len(minion_tasks)
		+ (len(valid_ward_skin_ids) * 4)
		+ len(esports_banner_tasks)
	)
	curr_op = 0

	for char_name, skins_dir, skin_id, target_base_id in turret_tasks:
		curr_op += 1
		base_file = skins_dir / f"skin{target_base_id}.bin.txt"
		try:
			base_text = base_file.read_text(encoding="utf-8", errors="ignore")
		except OSError:
			continue
		target_skin = f"Skin{skin_id}"
		text = fast_vanilla_revert(base_text, target_skin, skin_numeric_id=skin_id)
		out_file = skins_dir / f"skin{skin_id}.bin.txt"
		if not dry_run:
			out_file.write_text(text, encoding="utf-8")
		generated_files.append(out_file)
		if progress_callback:
			progress_callback(curr_op, total_operations, f"{char_name}/skin{skin_id}.bin")

	for char_name, skins_dir, skin_id, target_base_id in rubble_tasks:
		curr_op += 1
		base_file = skins_dir / f"skin{target_base_id}.bin.txt"
		try:
			base_text = base_file.read_text(encoding="utf-8", errors="ignore")
		except OSError:
			continue
		target_skin = f"Skin{skin_id}"
		text = fast_vanilla_revert(base_text, target_skin, skin_numeric_id=skin_id)
		out_file = skins_dir / f"skin{skin_id}.bin.txt"
		if not dry_run:
			out_file.write_text(text, encoding="utf-8")
		generated_files.append(out_file)
		if progress_callback:
			progress_callback(curr_op, total_operations, f"turretrubble/skin{skin_id}.bin")

	for char_name, skins_dir, skin_id, target_base_id in minion_tasks:
		curr_op += 1
		base_file = skins_dir / f"skin{target_base_id}.bin.txt"
		try:
			base_text = base_file.read_text(encoding="utf-8", errors="ignore")
		except OSError:
			continue
		target_skin = f"Skin{skin_id}"
		text = fast_vanilla_revert(base_text, target_skin, skin_numeric_id=skin_id)
		out_file = skins_dir / f"skin{skin_id}.bin.txt"
		if not dry_run:
			out_file.write_text(text, encoding="utf-8")
		generated_files.append(out_file)
		if progress_callback:
			progress_callback(curr_op, total_operations, f"{char_name}/skin{skin_id}.bin")

	for char_name, skins_dir, skin_id in standard_tasks:
		curr_op += 1
		base_file = skins_dir / "skin0.bin.txt"
		try:
			base_text = base_file.read_text(encoding="utf-8", errors="ignore")
		except OSError:
			continue
		target_skin = f"Skin{skin_id}"
		text = fast_vanilla_revert(base_text, target_skin, skin_numeric_id=skin_id)
		out_file = skins_dir / f"skin{skin_id}.bin.txt"
		if not dry_run:
			out_file.write_text(text, encoding="utf-8")
		generated_files.append(out_file)
		if progress_callback:
			progress_callback(curr_op, total_operations, f"{char_name}/skin{skin_id}.bin")

	if sightward_skins_dir:
		def _load_base_txt(s_dir: Path | None) -> str:
			if s_dir and (s_dir / "skin0.bin.txt").is_file():
				try:
					return (s_dir / "skin0.bin.txt").read_text(encoding="utf-8", errors="ignore")
				except OSError:
					return ""
			return ""

		sightward_base_txt = _load_base_txt(sightward_skins_dir)
		yellowtrinket_base_txt = _load_base_txt(yellowtrinket_skins_dir)
		jammerdevice_base_txt = _load_base_txt(jammerdevice_skins_dir)
		bluetrinket_base_txt = _load_base_txt(bluetrinket_skins_dir)

		if sightward_base_txt:
			for skin_id in valid_ward_skin_ids:
				curr_op += 1
				target_skin = f"Skin{skin_id}"
				text = fast_vanilla_revert(sightward_base_txt, target_skin, skin_numeric_id=None)
				out_file = sightward_skins_dir / f"skin{skin_id}.bin.txt"
				if not dry_run:
					out_file.write_text(text, encoding="utf-8")
				generated_files.append(out_file)
				if progress_callback:
					progress_callback(curr_op, total_operations, f"sightward/skin{skin_id}.bin")

		if yellowtrinket_base_txt and yellowtrinket_skins_dir:
			for skin_id in valid_ward_skin_ids:
				curr_op += 1
				target_skin = f"Skin{skin_id}"
				text = _expand_ward_skin0_only_hash("YellowTrinket", yellowtrinket_base_txt, target_skin)
				out_file = yellowtrinket_skins_dir / f"skin{skin_id}.bin.txt"
				if not dry_run:
					out_file.write_text(text, encoding="utf-8")
				generated_files.append(out_file)
				if progress_callback:
					progress_callback(curr_op, total_operations, f"yellowtrinket/skin{skin_id}.bin")

		if jammerdevice_base_txt and jammerdevice_skins_dir:
			for skin_id in valid_ward_skin_ids:
				curr_op += 1
				target_skin = f"Skin{skin_id}"
				text = _expand_ward_skin0_only_hash("JammerDevice", jammerdevice_base_txt, target_skin)
				out_file = jammerdevice_skins_dir / f"skin{skin_id}.bin.txt"
				if not dry_run:
					out_file.write_text(text, encoding="utf-8")
				generated_files.append(out_file)
				if progress_callback:
					progress_callback(curr_op, total_operations, f"jammerdevice/skin{skin_id}.bin")

		if bluetrinket_base_txt and bluetrinket_skins_dir:
			for skin_id in valid_ward_skin_ids:
				curr_op += 1
				target_skin = f"Skin{skin_id}"
				text = _expand_ward_skin0_only_hash("BlueTrinket", bluetrinket_base_txt, target_skin)
				out_file = bluetrinket_skins_dir / f"skin{skin_id}.bin.txt"
				if not dry_run:
					out_file.write_text(text, encoding="utf-8")
				generated_files.append(out_file)
				if progress_callback:
					progress_callback(curr_op, total_operations, f"bluetrinket/skin{skin_id}.bin")

	if esports_banner_tasks:
		for b_name, b_dir, tgt_filename in esports_banner_tasks:
			curr_op += 1
			target_file = b_dir / tgt_filename
			if not target_file.is_file() and (b_dir.parent / tgt_filename).is_file():
				target_file = b_dir.parent / tgt_filename

			try:
				txt_content = target_file.read_text(encoding="utf-8", errors="ignore")
			except OSError:
				continue

			cleaned, c = _sanitize_arena_banner_text(txt_content)
			if c > 0:
				if not dry_run:
					target_file.write_text(cleaned, encoding="utf-8")
				generated_files.append(target_file)
				LOG.info("Sanitized Arena Banner textures (%d changes): %s/%s", c, b_name, tgt_filename)

			if progress_callback:
				progress_callback(curr_op, total_operations, f"{b_name}/{tgt_filename[:-4]}")

	return generated_files


# ==============================================================================
# LEGACY MULTIPROCESSING CHAMPION WORKER POOL (PARALLEL TELEMETRY 1:1)
# ==============================================================================
def _process_one_champion_wad_job(args: tuple[Path, dict, Path, bool, object]) -> tuple[Path, str]:
	wad_path, tools, cache_dir, dry_run, progress_queue = args
	wad_hash = file_fingerprint(wad_path)
	script_hash = _script_fingerprint()
	wad_cache_dir = get_canonical_cache_dir(cache_dir, wad_path)

	if _is_cache_valid(wad_cache_dir, wad_hash, script_hash) and not dry_run:
		progress_queue.put((wad_path.name, 1.0, "cache hit", wad_path.name))
		return wad_cache_dir, wad_path.name

	def emit_file_progress(item: int, count: int, name: str, base_ratio: float, span: float, stage: str) -> None:
		fraction = base_ratio + (item / max(count, 1)) * span
		progress_queue.put((wad_path.name, fraction, f"{stage} {item}/{count}", name))

	with tempfile.TemporaryDirectory(dir=str(get_temp_workspace()), prefix=f"vanilla_champ_{wad_path.stem}_") as temp_dir_str:
		temp_dir = Path(temp_dir_str)
		progress_queue.put((wad_path.name, 0.05, "extracting WAD", wad_path.name))
		
		extract_wad(
			wad_path,
			temp_dir,
			tools,
			filter_type=None,
			filter_pattern=r"(?i)(skins/skin.*\.bin$|skins/base/.*\.skn$)",
			dry_run=dry_run,
		)

		def base_decompile_selector(source_d: Path) -> list[Path]:
			return [p for p in _iter_champion_files(source_d) if p.stem.lower() == "skin0"]

		decompiled_count = decompile_bin_files(
			temp_dir,
			tools,
			dry_run=dry_run,
			file_selector=base_decompile_selector,
			parallel=False,
			progress_callback=lambda item, count, name: emit_file_progress(item, count, name, 0.10, 0.20, "decompiling"),
		)
		if decompiled_count == 0 and not dry_run:
			progress_queue.put((wad_path.name, 1.0, "no skins available", wad_path.name))
			return wad_cache_dir, wad_path.name

		clean_champ_name = wad_path.name.split(".")[0]
		progress_queue.put((wad_path.name, 0.30, "reverting skins to vanilla", wad_path.name))
		generated_files, submesh_alias_tasks = force_vanilla_existing_skins(
			temp_dir,
			workspace_root=tools["workspace_root"],
			main_entity=clean_champ_name,
			dry_run=dry_run,
			progress_callback=lambda item, count, name: emit_file_progress(item, count, name, 0.30, 0.20, "reverting"),
		)

		if not generated_files:
			progress_queue.put((wad_path.name, 1.0, "no additional changes", wad_path.name))
			return wad_cache_dir, wad_path.name

		recompile_text_to_bin(
			temp_dir,
			tools,
			dry_run=dry_run,
			file_selector=lambda _: generated_files,
			parallel=False,
			progress_callback=lambda item, count, name: emit_file_progress(item, count, name, 0.50, 0.45, "recompiling"),
		)

		if not dry_run:
			shutil.rmtree(wad_cache_dir, ignore_errors=True)
			wad_cache_dir.mkdir(parents=True, exist_ok=True)
			copied = 0
			for gen_txt in generated_files:
				out_bin = gen_txt.with_name(gen_txt.name[:-4])
				if out_bin.is_file():
					rel = out_bin.relative_to(temp_dir)
					dest = wad_cache_dir / rel
					dest.parent.mkdir(parents=True, exist_ok=True)
					shutil.copy2(out_bin, dest)
					copied += 1

			if submesh_alias_tasks:
				for ent_name, skin_sfx, old_sub, new_sub in submesh_alias_tasks:
					base_skn_list = [p for p in temp_dir.rglob("*.skn") if "base" in p.as_posix().lower()]
					if not base_skn_list:
						base_skn_list = list(temp_dir.rglob(f"{ent_name}.skn")) + list(temp_dir.rglob(f"{ent_name.capitalize()}.skn"))

					if base_skn_list:
						raw_skn_bytes = base_skn_list[0].read_bytes()
						patched_bytes = patch_skn_submesh_inplace(raw_skn_bytes, old_sub, new_sub)
						patched_name = f"{ent_name.capitalize()}_Skin{skin_sfx}.skn"
						dest_skn = wad_cache_dir / "assets" / "characters" / ent_name / "skins" / "base" / patched_name
						dest_skn.parent.mkdir(parents=True, exist_ok=True)
						dest_skn.write_bytes(patched_bytes)
						copied += 1
						LOG.info("Applied in-place submesh patch for %s Skin%s: %s -> %s", ent_name, skin_sfx, old_sub, new_sub)

			if copied > 0:
				_write_cache_hash(wad_cache_dir, wad_hash, script_hash)

	progress_queue.put((wad_path.name, 1.0, "completed", wad_path.name))
	return wad_cache_dir, wad_path.name


def process_champion_wads(
	champion_wads: list[Path],
	tools: dict,
	cache_dir: Path,
	dry_run: bool = False,
	progress_callback: Callable[[float, str, str], None] | None = None,
) -> list[tuple[Path, str]]:
	champion_wads = list(champion_wads)
	total = len(champion_wads)
	if not total:
		return []

	outputs: dict[str, tuple[Path, str]] = {}
	pending: list[Path] = []
	wad_progress: dict[str, float] = {}

	script_hash = _script_fingerprint()
	for wad_path in champion_wads:
		wad_hash = file_fingerprint(wad_path)
		wad_cache_dir = get_canonical_cache_dir(cache_dir, wad_path)
		if _is_cache_valid(wad_cache_dir, wad_hash, script_hash) and not dry_run:
			outputs[wad_path.name.lower()] = (wad_cache_dir, wad_path.name)
			wad_progress[wad_path.name.lower()] = 1.0
		else:
			pending.append(wad_path)
			wad_progress[wad_path.name.lower()] = 0.0

	max_workers = min(MAX_PARALLEL_WAD_JOBS, len(pending))
	if pending:
		with multiprocessing.Manager() as manager:
			progress_queue = manager.Queue()
			jobs = [(
				wad_path,
				tools,
				Path(cache_dir),
				dry_run,
				progress_queue,
			) for wad_path in pending]
			with ProcessPoolExecutor(max_workers=max_workers) as executor:
				future_map = {
					executor.submit(_process_one_champion_wad_job, job): wad_path
					for job, wad_path in zip(jobs, pending)
				}
				remaining = set(future_map)
				while remaining:
					while True:
						try:
							name, fraction, detail, curr_file = progress_queue.get_nowait()
							wad_progress[name.lower()] = fraction
							if progress_callback:
								overall = sum(wad_progress.values()) / max(total, 1)
								completed = sum(1 for v in wad_progress.values() if v >= 1.0)
								progress_callback(
									overall,
									f"({completed}/{total})",
									f"{name} › {curr_file}",
								)
						except queue.Empty:
							break

					done, remaining = wait(remaining, timeout=0.1, return_when=FIRST_COMPLETED)
					for future in done:
						wad_path = future_map[future]
						try:
							result = future.result()
						except Exception as exc:
							raise RuntimeError(f"Failed to process {wad_path.name}: {exc}") from exc
						outputs[wad_path.name.lower()] = result
						wad_progress[wad_path.name.lower()] = 1.0

	return [outputs[wad_path.name.lower()] for wad_path in champion_wads]


# ==============================================================================
# LEGACY STANDALONE .FANTOME PACKAGING (DIRECT FLAT WAD HARVESTING)
# ==============================================================================
def create_standalone_fantome(
	cache_root: Path,
	fantome_path: Path,
	allowed_wads: set[str] | None = None,
	options: dict[str, bool] | None = None,
	dry_run: bool = False,
	progress_callback: Callable[[float, str, str, str], None] | None = None,
) -> Path:
	fantome_path = Path(fantome_path)
	fantome_path.parent.mkdir(parents=True, exist_ok=True)

	final_cache = Path(cache_root)
	final_cache.mkdir(parents=True, exist_ok=True)

	allowed_lower = {w.lower() for w in allowed_wads} if allowed_wads else None

	opt_labels = {
		"champions": "Champions",
		"structures": "Structures",
		"minions": "Minions",
		"wards": "Wards",
		"accents": "Map Accents",
		"finishers": "Nexus Finishers",
		"recalls": "Special Recalls",
	}
	active_modules = [label for key, label in opt_labels.items() if (options or {}).get(key, False)]
	desc_str = f"ACTIVE MODULES: {', '.join(active_modules)}." if active_modules else "Vanilla True Mod - Custom Build."

	metadata = {
		"Name": "Vanilla True Mod",
		"Author": "Neve",
		"Version": APP_VERSION,
		"Description": desc_str,
		"License": "CC-BY-NC-SA-4.0",
		"Tags": ["Vanilla","Restoration"],
		"Categories": ["Vanilla","Restoration"],
	}
	info_json_bytes = json.dumps(metadata, indent=2, ensure_ascii=False).encode("utf-8")

	readme_md = f"""### 📖 Overview
A surgical, zero-bloat restoration mod that brings League of Legends back to its canonical **Vanilla** state using strictly native engine declarations and memory graphs.

---

### 🌟 Acknowledgements & Dedications
This project stands on the shoulders of the true pioneers of the League of Legends reverse engineering and modding community:

* **[moonshadow565](https://github.com/moonshadow565)** — Creator of **`ritobin`** and **`lolpytools`**.
* **[Crauzer](https://github.com/Crauzer)** — Head of **`LeagueToolkit`**, **`cslol-manager`**, and WAD toolchains.
* **[CommunityDragon](https://github.com/CommunityDragon)** — For continuous maintenance of game hash tables.

---

Compiled and packaged via **[Vanilla True Builder](https://github.com/gsmeven/vanilla-true-builder)** by **[Neve](https://github.com/gsmeven)**.
"""

	license_text = """Copyright (C) 2026 Neve — Vanilla True Builder
	
Licensed under Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International.
Full Terms: https://creativecommons.org/licenses/by-nc-sa/4.0/
	
Permissions & Conditions:
• Attribution (BY): Credit the author (Neve) and link to the upstream repository.
• Non-Commercial (NC): Commercial use, monetization, paywalls, or resale are strictly prohibited.
• Share-Alike (SA): Any modifications, updates, or derivative builds must inherit this exact license.
	
Disclaimer:
Provided "AS IS", without warranties of any kind. Unofficial community project;
League of Legends and all related game assets belong to Riot Games, Inc.
"""

	archive_entries: list[tuple[Path, str]] = []
	seen_targets: set[str] = set()

	target_wad_dirs = [
		d for d in final_cache.iterdir()
		if d.is_dir() and d.name.lower().endswith(".wad.client")
	]

	for wad_folder in target_wad_dirs:
		wad_name = wad_folder.name
		wad_lower = wad_name.lower()

		if allowed_lower and wad_lower not in allowed_lower:
			continue

		for current, _, filenames in os.walk(str(wad_folder)):
			current_path = Path(current)
			for filename in filenames:
				if filename.lower().endswith(".txt") or filename == ".hash":
					continue

				file_path = current_path / filename
				relative_to_wad = _normalize_asset_path(file_path.relative_to(wad_folder))

				if not _is_mod_target_file(wad_name, relative_to_wad, options=options):
					continue

				clean_rel = re.sub(r'(?i)\.ltk(?:\.[a-z0-9_]+)?$', '', relative_to_wad)
				arcname = f"WAD/{wad_name}/{clean_rel}"

				if arcname in seen_targets:
					continue
				seen_targets.add(arcname)
				archive_entries.append((file_path, arcname))

	total_entries = max(1, len(archive_entries))
	if not dry_run and not archive_entries:
		active_opts = [k for k, v in (options or {}).items() if v]
		raise RuntimeError(f"No modified resources were found in cache for packaging. Active modules: {active_opts}")

	wads_included = {arc.split('/')[1] for _, arc in archive_entries}
	LOG.info("Packaging %d files across %d WADs: %s", len(archive_entries), len(wads_included), sorted(wads_included))

	if not dry_run:
		preview_img = None
		search_dirs = [DEFAULT_WORKSPACE_ROOT]
		if hasattr(sys, "_MEIPASS"):
			search_dirs.insert(0, Path(getattr(sys, "_MEIPASS")))

		for s_dir in search_dirs:
			for img_name in ("preview.png", "preview.jpg", "banner.png", "image.png", "icon.png"):
				candidate = s_dir / img_name
				if candidate.is_file():
					preview_img = candidate
					break
			if preview_img:
				break

		with zipfile.ZipFile(fantome_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
			archive.writestr("META/info.json", info_json_bytes)
			archive.writestr("META/README.md", readme_md.encode("utf-8"))
			archive.writestr("META/LICENSE", license_text.encode("utf-8"))

			if preview_img:
				archive.write(preview_img, "META/image.png")
				archive.write(preview_img, "META/icon.png")

			for index, (file_path, arcname) in enumerate(archive_entries, start=1):
				archive.write(file_path, arcname)
				if progress_callback and (index % 100 == 0 or index == total_entries):
					progress_callback(index / total_entries, "Package", f"({index}/{total_entries})", arcname)

	return fantome_path


def _resolve_entity_to_champion_wad(entity: str, champion_wads: list[Path]) -> Path | None:
	ent_lower = entity.lower()
	for wad in champion_wads:
		c_name = wad.name.split(".")[0].lower()
		if c_name == ent_lower:
			return wad
	for wad in champion_wads:
		c_name = wad.name.split(".")[0].lower()
		if ent_lower.startswith(c_name) or c_name.startswith(ent_lower):
			return wad
	for wad in champion_wads:
		c_name = wad.name.split(".")[0].lower()
		if len(c_name) >= 5 and c_name[:5] in ent_lower:
			return wad
	return None


# ==============================================================================
# LEAGUETOOLKIT (LTK) NATIVE MOD PROJECT BUILDERS (8 LAYERS)
# ==============================================================================
def init_mod_project_structure(
	project_dir: Path,
	workspace_root: Path = DEFAULT_WORKSPACE_ROOT,
	options: dict[str, bool] | None = None,
) -> None:
	if project_dir.exists():
		shutil.rmtree(project_dir, ignore_errors=True)
	project_dir.mkdir(parents=True, exist_ok=True)

	layer_names = [
		("base", 0, "Base transparent textures"),
		("champions", 10, "Champion skins vanilla normalization"),
		("structures", 20, "Canonical structure models and rubble"),
		("minions", 30, "Canonical faction minion silhouettes"),
		("wards", 40, "Vision wards and trinkets normalization"),
		("accents", 50, "Map clutter and trophy neutralization"),
		("finishers", 60, "Victory/Defeat cinematic finishers neutralization"),
		("recalls", 70, "Special recall overlays normalization"),
	]

	layers_payload = []
	for name, priority, desc in layer_names:
		(project_dir / "content" / name).mkdir(parents=True, exist_ok=True)
		layers_payload.append({
			"name": name,
			"priority": priority,
			"description": desc,
		})

	desc_str = "Select your desired modules below in 'Layers' to restore them to Vanilla."

	search_dirs = [workspace_root]
	if hasattr(sys, "_MEIPASS"):
		search_dirs.insert(0, Path(getattr(sys, "_MEIPASS")))

	art_icon = None
	art_image = None
	for s_dir in search_dirs:
		for img_name in ("preview.png", "preview.jpg", "preview.jpeg", "banner.png", "image.png", "icon.png"):
			cand = s_dir / img_name
			if cand.is_file():
				try:
					ext = cand.suffix.lower()
					art_icon = f"icon{ext}"
					art_image = f"image{ext}"
					shutil.copy2(cand, project_dir / art_icon)
					shutil.copy2(cand, project_dir / art_image)
					for meta_folder in ("meta", "META"):
						meta_dir = project_dir / meta_folder
						meta_dir.mkdir(parents=True, exist_ok=True)
						shutil.copy2(cand, project_dir / art_icon)
						shutil.copy2(cand, project_dir / art_image)
						shutil.copy2(cand, project_dir / f"thumbnail{ext}")
					break
				except Exception:
					pass
		if art_icon:
			break

	semver = APP_VERSION.lstrip("v")
	manifest = {
		"name": "vanilla-true",
		"display_name": "Vanilla True Mod",
		"version": semver,
		"description": desc_str,
		"authors": ["Neve"],
		"license": "CC-BY-NC-SA-4.0",
		"tags": ["Vanilla","Restoration"],
		"layers": layers_payload,
	}

	if art_icon and art_image:
		manifest["thumbnail"] = f"thumbnail{ext}"
		manifest["icon"] = art_icon
		manifest["image"] = art_image

	(project_dir / "mod.config.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
	(project_dir / ".modignore").write_text("*.tmp\n*.bak\n.git*\nThumbs.db\n.hash\n", encoding="utf-8")

	readme_md = f"""### 📖 Overview
A surgical, zero-bloat restoration mod that brings League of Legends back to its canonical **Vanilla** state using strictly native engine declarations and memory graphs.

---

### 🌟 Acknowledgements & Dedications
This project stands on the shoulders of the true pioneers of the League of Legends reverse engineering and modding community:

* **[moonshadow565](https://github.com/moonshadow565)** — Creator of **`ritobin`** and **`lolpytools`**.
* **[Crauzer](https://github.com/Crauzer)** — Head of **`LeagueToolkit`**, **`cslol-manager`**, and WAD toolchains.
* **[CommunityDragon](https://github.com/CommunityDragon)** — For continuous maintenance of game hash tables.

---

Compiled and packaged via **[Vanilla True Builder](https://github.com/gsmeven/vanilla-true-builder)** by **[Neve](https://github.com/gsmeven)**.
"""
	(project_dir / "README.md").write_text(readme_md, encoding="utf-8")

	license_text = """Copyright (C) 2026 Neve — Vanilla True Builder

Licensed under Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International.
Full Terms: https://creativecommons.org/licenses/by-nc-sa/4.0/

Permissions & Conditions:
• Attribution (BY): Credit the author (Neve) and link to the upstream repository.
• Non-Commercial (NC): Commercial use, monetization, paywalls, or resale are strictly prohibited.
• Share-Alike (SA): Any modifications, updates, or derivative builds must inherit this exact license.

Disclaimer:
Provided "AS IS", without warranties of any kind. Unofficial community project;
League of Legends and all related game assets belong to Riot Games, Inc.
"""
	(project_dir / "LICENSE").write_text(license_text, encoding="utf-8")
	(project_dir / "LICENSE.md").write_text(license_text, encoding="utf-8")

	for meta_folder in ("meta", "META"):
		meta_dir = project_dir / meta_folder
		meta_dir.mkdir(parents=True, exist_ok=True)
		(meta_dir / "LICENSE").write_text(license_text, encoding="utf-8")
		(meta_dir / "README.md").write_text(readme_md, encoding="utf-8")


def build_layer_base(project_dir: Path) -> None:
	content_base = project_dir / "content" / "base"
	for wad_name in ("Global.wad.client", "Map11.wad.client"):
		tex_dest = content_base / wad_name / "assets" / "transparent.tex"
		tex_dest.parent.mkdir(parents=True, exist_ok=True)
		tex_dest.write_bytes(RAW_TRANSPARENT_TEX_BYTES)


def build_layer_champions(
	champion_wads: list[Path],
	project_dir: Path,
	cache_dir: Path,
	map11_cache_dir: Path | None = None,
	dry_run: bool = False,
) -> None:
	if dry_run:
		return
	content_champ = project_dir / "content" / "champions"
	for wad_path in champion_wads:
		wad_cache_dir = get_canonical_cache_dir(cache_dir, wad_path)
		if not wad_cache_dir.is_dir():
			continue
		dest_wad_dir = content_champ / wad_path.name
		for cached_file in wad_cache_dir.rglob("*"):
			if cached_file.is_file() and cached_file.name != ".hash" and not cached_file.name.endswith(".txt"):
				rel = cached_file.relative_to(wad_cache_dir)
				dest_f = dest_wad_dir / rel
				dest_f.parent.mkdir(parents=True, exist_ok=True)
				shutil.copy2(cached_file, dest_f)

	if map11_cache_dir and map11_cache_dir.is_dir():
		dest_map_champ = content_champ / "Map11.wad.client"
		for char_name in MAP_CHAMPION_CHARACTERS:
			for char_root in (map11_cache_dir / "data" / "characters", map11_cache_dir / "characters"):
				skins_dir = char_root / char_name / "skins"
				if skins_dir.is_dir():
					for bin_file in skins_dir.glob("skin*.bin"):
						if bin_file.is_file() and bin_file.stem.lower() != "skin0":
							rel = bin_file.relative_to(map11_cache_dir)
							dest = dest_map_champ / rel
							dest.parent.mkdir(parents=True, exist_ok=True)
							shutil.copy2(bin_file, dest)


def build_layer_structures(
	project_dir: Path,
	map11_cache_dir: Path,
	dry_run: bool = False,
) -> None:
	if dry_run or not map11_cache_dir.is_dir():
		return
	content_struct = project_dir / "content" / "structures" / "Map11.wad.client"
	for char_name in STRUCTURE_CHARACTERS:
		for char_root in (map11_cache_dir / "data" / "characters", map11_cache_dir / "characters"):
			skins_dir = char_root / char_name / "skins"
			if skins_dir.is_dir():
				for bin_file in skins_dir.glob("skin*.bin"):
					if bin_file.is_file():
						m = re.fullmatch(r"skin(\d+)\.bin", bin_file.name, re.IGNORECASE)
						if not m:
							continue
						skin_id = m.group(1)
						if char_name in {"turret", "inhibitor", "nexus"} and skin_id in {"0", "1", "2", "3"}:
							continue
						if char_name == "turretrubble" and skin_id in {"0", "1"}:
							continue
						if skin_id == "0":
							continue
						rel = bin_file.relative_to(map11_cache_dir)
						dest = content_struct / rel
						dest.parent.mkdir(parents=True, exist_ok=True)
						shutil.copy2(bin_file, dest)


def build_layer_minions(
	project_dir: Path,
	map11_cache_dir: Path,
	dry_run: bool = False,
) -> None:
	if dry_run or not map11_cache_dir.is_dir():
		return
	content_minions = project_dir / "content" / "minions" / "Map11.wad.client"
	for char_name in MINION_CHARACTERS:
		for char_root in (map11_cache_dir / "data" / "characters", map11_cache_dir / "characters"):
			skins_dir = char_root / char_name / "skins"
			if skins_dir.is_dir():
				for bin_file in skins_dir.glob("skin*.bin"):
					if bin_file.is_file():
						m = re.fullmatch(r"skin(\d+)\.bin", bin_file.name, re.IGNORECASE)
						if not m:
							continue
						skin_id = m.group(1)
						if skin_id in {"0", "1"}:
							continue
						rel = bin_file.relative_to(map11_cache_dir)
						dest = content_minions / rel
						dest.parent.mkdir(parents=True, exist_ok=True)
						shutil.copy2(bin_file, dest)


def build_layer_wards(
	project_dir: Path,
	map11_cache_dir: Path,
	dry_run: bool = False,
) -> None:
	if dry_run or not map11_cache_dir.is_dir():
		return
	content_wards = project_dir / "content" / "wards" / "Map11.wad.client"
	for ward_name in WARD_CHARACTERS:
		for char_root in (map11_cache_dir / "data" / "characters", map11_cache_dir / "characters"):
			skins_dir = char_root / ward_name / "skins"
			if skins_dir.is_dir():
				for bin_file in skins_dir.glob("skin*.bin"):
					if bin_file.is_file() and bin_file.stem.lower() != "skin0":
						rel = bin_file.relative_to(map11_cache_dir)
						dest = content_wards / rel
						dest.parent.mkdir(parents=True, exist_ok=True)
						shutil.copy2(bin_file, dest)


def build_layer_accents(
	project_dir: Path,
	map11_cache_dir: Path,
	dry_run: bool = False,
) -> None:
	if dry_run or not map11_cache_dir.is_dir():
		return
	content_accents = project_dir / "content" / "accents" / "Map11.wad.client"
	for current, _, filenames in os.walk(str(map11_cache_dir)):
		for f in filenames:
			if f.endswith(".txt") or f == ".hash":
				continue
			p = Path(current) / f
			rel = p.relative_to(map11_cache_dir)
			rel_lower = rel.as_posix().lower()

			is_accent = (
				"base_srx.materials.bin" in rel_lower
				or rel_lower.endswith("map11.bin")
				or any(ev in rel_lower for ev in EVENT_CHARACTERS)
				or any(bn in rel_lower for bn in BANNER_CHARACTERS)
			)
			if is_accent:
				dest = content_accents / rel
				dest.parent.mkdir(parents=True, exist_ok=True)
				shutil.copy2(p, dest)


def build_layer_finishers(
	project_dir: Path,
	ui_cache_dir: Path,
	dry_run: bool = False,
) -> None:
	if dry_run or not ui_cache_dir.is_dir():
		return
	content_fin = project_dir / "content" / "finishers" / ui_cache_dir.name
	for bin_file in ui_cache_dir.rglob("*.bin"):
		if bin_file.is_file() and bin_file.name != ".hash":
			rel = bin_file.relative_to(ui_cache_dir)
			dest = content_fin / rel
			dest.parent.mkdir(parents=True, exist_ok=True)
			shutil.copy2(bin_file, dest)


def pack_modpkg_project(
	project_dir: Path,
	output_dir: Path,
	tools: dict,
	progress_callback: Callable[[float, str, str, str], None] | None = None,
) -> Path:
	output_dir.mkdir(parents=True, exist_ok=True)
	league_mod = tools.get("league_mod", tools["tools_dir"] / "league-mod.exe")
	if not league_mod.is_file():
		raise FileNotFoundError(f"league-mod.exe not found at {league_mod}")

	config_file = project_dir / "mod.config.json"

	all_files = [p for p in project_dir.rglob("*") if p.is_file() and not p.name.endswith(".tmp")]
	total = max(1, len(all_files))

	for idx, f in enumerate(all_files, start=1):
		rel = f.relative_to(project_dir).as_posix()
		if progress_callback:
			progress_callback(0.1 + (idx / total) * 0.7, "Package", f"({idx}/{total})", rel)

	def on_cmd_output(line: str):
		if progress_callback:
			progress_callback(0.85, "Package", f"({total}/{total})", f"league-mod › {line}")

	for f in list(project_dir.rglob("*")):
		if f.is_file():
			clean_name = re.sub(r'(?i)\.ltk(?:\.[a-z0-9_]+)?$', '', f.name)
			if clean_name != f.name:
				f.replace(f.with_name(clean_name))

	cmd = [
		str(league_mod), "pack",
		"-c", str(config_file),
		"-f", "VanillaTrueMod",
		"-o", str(output_dir),
		"--format", "modpkg",
	]
	run_cmd(cmd, cwd=project_dir, progress_callback=on_cmd_output)

	expected_file = output_dir / "VanillaTrueMod.modpkg"
	if expected_file.is_file():
		if progress_callback:
			progress_callback(1.0, "Package", f"({total}/{total})", expected_file.name)
		return expected_file

	candidates = list(output_dir.glob("*.modpkg"))
	if candidates:
		if progress_callback:
			progress_callback(1.0, "Package", f"({total}/{total})", candidates[0].name)
		return candidates[0]

	raise RuntimeError(f"Package output was not created at {output_dir}")

# ==============================================================================
# DIRECTORY LIFECYCLE & SIGNALS (MAINPROCESS EXCLUSIVE)
# ==============================================================================
def cleanup_temp_dirs() -> None:
	if multiprocessing.current_process().name != "MainProcess":
		return
	if DEFAULT_TEMP_DIR.is_dir():
		try:
			shutil.rmtree(DEFAULT_TEMP_DIR, ignore_errors=True)
		except Exception as exc:
			LOG.warning("Failed to remove temporary workspace %s: %s", DEFAULT_TEMP_DIR, exc)


def cleanup_orphaned_temp_dirs() -> None:
	if multiprocessing.current_process().name != "MainProcess":
		return
	if not DEFAULT_TEMP_DIR.is_dir():
		return

	candidates: list[Path] = []
	for pattern in ("vanilla_builder_*", "vanilla_fantome_*", "vanilla_map11_*", "vanilla_champ_*", "vanilla_finisher_*", "vtb_*"):
		candidates.extend(DEFAULT_TEMP_DIR.glob(pattern))

	now = time.time()
	for temp_dir in sorted({p.resolve() for p in candidates}, key=lambda p: (len(p.parts), str(p))):
		if not temp_dir.is_dir():
			continue
		try:
			if (now - temp_dir.stat().st_mtime) < 600:
				continue
			shutil.rmtree(temp_dir, ignore_errors=True)
		except OSError as exc:
			LOG.warning("Could not remove orphaned directory %s: %s", temp_dir, exc)


def _handle_interrupt(signum, _frame):
	if multiprocessing.current_process().name == "MainProcess":
		LOG.warning("Interrupt received (signal %s). Cleaning workspace...", signum)
		cleanup_temp_dirs()
	raise SystemExit(128 + signum)


signal.signal(signal.SIGINT, _handle_interrupt)
if hasattr(signal, "SIGTERM"):
	signal.signal(signal.SIGTERM, _handle_interrupt)

atexit.register(cleanup_temp_dirs)


def resolve_default_paths() -> dict[str, Path | None]:
	return {
		"league_root": DEFAULT_LEAGUE_ROOT,
		"final_dir": DEFAULT_FINAL_DIR,
		"map_wad": DEFAULT_MAP_WAD,
		"ui_wad": DEFAULT_UI_WAD,
		"champions_dir": DEFAULT_CHAMPIONS_DIR,
		"workspace_root": DEFAULT_WORKSPACE_ROOT,
		"tools_dir": DEFAULT_WORKSPACE_ROOT / "tools",
		"project_dir": DEFAULT_PROJECT_DIR,
	}


def setup_workspace(
	base_dir: Path = Path.cwd(),
	force_hashes: bool = False,
	progress_callback: Callable[[str], None] | None = None,
	sync_tools: bool = True,
	sync_hashes: bool = True,
) -> dict:
	base_dir = Path(base_dir)
	cleanup_orphaned_temp_dirs()
	tools_dir = base_dir / "tools"

	if sync_tools:
		ensure_toolkit_binaries(tools_dir, progress_callback=progress_callback)

	wadtools = tools_dir / "wadtools.exe"
	ritobin = tools_dir / "ritobin.exe"
	league_mod = tools_dir / "league-mod.exe"
	hashes_dir = tools_dir / "hashes"

	if not wadtools.exists():
		LOG.warning("wadtools.exe not found at %s", wadtools)
	if not ritobin.exists():
		LOG.warning("ritobin.exe not found at %s", ritobin)
	if not league_mod.exists():
		LOG.warning("league-mod.exe not found at %s", league_mod)

	if sync_hashes:
		ensure_ritobin_hashes(hashes_dir, force=force_hashes, progress_callback=progress_callback)

	cache_dir = base_dir / "cache"
	cache_dir.mkdir(parents=True, exist_ok=True)

	overrides_dir = base_dir / "overrides"
	overrides_dir.mkdir(parents=True, exist_ok=True)

	project_dir = get_temp_workspace() / "VanillaTrue_Project"

	return {
		"base_dir": base_dir,
		"workspace_root": base_dir,
		"tools_dir": tools_dir,
		"wadtools": wadtools,
		"ritobin": ritobin,
		"league_mod": league_mod,
		"hashes_dir": hashes_dir,
		"final_dir": DEFAULT_FINAL_DIR,
		"champions_dir": DEFAULT_CHAMPIONS_DIR,
		"champion_wads": _iter_champion_wads(),
		"cache_dir": cache_dir,
		"overrides_dir": overrides_dir,
		"project_dir": project_dir,
	}


# ==============================================================================
# DYNAMIC PROGRESS SCHEDULER
# ==============================================================================
class ProgressScheduler:
	def __init__(self, options: dict[str, bool], map_wad: Path, ui_wad: Path | None, champion_wads: list[Path]):
		weights: dict[str, float] = {
			"tools": 1.5,
			"hashes": 2.5,
		}

		needs_map = any([
			options.get("accents", True),
			options.get("wards", True),
			options.get("structures", True),
			options.get("minions", True),
			options.get("finishers", True),
			options.get("champions", True),
		]) and map_wad.is_file()

		if needs_map:
			weights["map11"] = 35.0

		needs_ui_or_global = (
			options.get("finishers", True) or options.get("accents", True) or options.get("recalls", True)
		) and (ui_wad is not None or (DEFAULT_FINAL_DIR / "Global.wad.client").is_file())

		if needs_ui_or_global:
			weights["ui"] = 5.0

		if options.get("champions", True) and len(champion_wads) > 0:
			weights["champions"] = 50.0

		weights["package"] = 8.0

		total_weight = sum(weights.values())
		self.spans: dict[str, tuple[float, float]] = {}
		curr = 0.0
		for stage, w in weights.items():
			span = (w / total_weight) * 100.0
			self.spans[stage] = (curr, curr + span)
			curr += span

	def pct(self, stage: str, sub_ratio: float = 0.0) -> float:
		start, end = self.spans.get(stage, (0.0, 100.0))
		sub = max(0.0, min(1.0, sub_ratio))
		return round(start + sub * (end - start), 1)


# ==============================================================================
# UNIFIED PIPELINE ORCHESTRATOR (DUAL ENGINE: .MODPKG & .FANTOME LEGACY)
# ==============================================================================
def build_vanilla_pipeline(
	map_wad: Path,
	workspace_root: Path,
	ui_wad: Path | None = None,
	options: dict[str, bool] | None = None,
	dry_run: bool = False,
	progress_callback: Callable[[float, str, str, str], None] | None = None,
	cfg: dict | None = None,
	scheduler: ProgressScheduler | None = None,
	package_format: str = "modpkg",
) -> dict:
	if cfg is None:
		cfg = setup_workspace(workspace_root)

	if options is None:
		options = {
			"accents": True,
			"wards": True,
			"structures": True,
			"minions": True,
			"finishers": True,
			"recalls": True,
			"champions": True,
		}

	opt_accents = options.get("accents", True)
	opt_wards = options.get("wards", True)
	opt_structures = options.get("structures", True)
	opt_minions = options.get("minions", True)
	opt_finishers = options.get("finishers", True)
	opt_recalls = options.get("recalls", True)
	opt_champions = options.get("champions", True)

	champion_wads = cfg.get("champion_wads", [])
	if scheduler is None:
		scheduler = ProgressScheduler(options, map_wad, ui_wad, champion_wads)

	def emit_progress(percent: float, phase: str, status: str, current: str = "") -> None:
		if progress_callback:
			progress_callback(percent, phase, status, current)

	try:
		# ======================================================================
		# ENGINE 1: NATIVE LTK MODULAR PROJECT PIPELINE (.MODPKG)
		# ======================================================================
		if package_format.lower() == "modpkg":
			project_dir = cfg["project_dir"]
			global_wad = cfg["final_dir"] / "Global.wad.client"
			cache_dir = cfg["cache_dir"]
			script_hash = _script_fingerprint()

			if not dry_run:
				init_mod_project_structure(project_dir, workspace_root=workspace_root, options=options)

			# 1. MAP11 CONTAINER & SUB-MODULES
			needs_map = any([opt_accents, opt_wards, opt_structures, opt_minions, opt_finishers, opt_champions]) and map_wad.is_file()
			if needs_map:
				emit_progress(scheduler.pct("map11", 0.0), "PROCESSING MAP11", "", "Verifying canonical Map11 container...")
				map11_hash = file_fingerprint(map_wad)
				map11_cache_dir = get_canonical_cache_dir(cache_dir, map_wad)

				if _is_cache_valid(map11_cache_dir, map11_hash, script_hash) and not dry_run:
					LOG.info("Reusing Map11 from canonical master cache: %s", map_wad.name)
					emit_progress(scheduler.pct("map11", 0.5), "Map11: Cache", "", "Valid Map11 master cache reused...")
					if opt_structures:
						build_layer_structures(project_dir, map11_cache_dir, dry_run=dry_run)
					if opt_minions:
						build_layer_minions(project_dir, map11_cache_dir, dry_run=dry_run)
					if opt_wards:
						build_layer_wards(project_dir, map11_cache_dir, dry_run=dry_run)
					if opt_accents:
						build_layer_accents(project_dir, map11_cache_dir, dry_run=dry_run)
				else:
					with tempfile.TemporaryDirectory(dir=str(get_temp_workspace()), prefix="vanilla_map11_") as temp_dir_str:
						temp_dir = Path(temp_dir_str)
						emit_progress(scheduler.pct("map11", 0.05), "Map11: Extract", "", f"Extracting binaries from {map_wad.name}...")

						extract_wad(
							map_wad,
							temp_dir,
							cfg,
							filter_type="bin",
							dry_run=dry_run,
							progress_callback=lambda status: emit_progress(scheduler.pct("map11", 0.08), "Map11: Extract", "", status),
						)

						decompile_bin_files(
							temp_dir,
							cfg,
							dry_run=dry_run,
							file_selector=_iter_map11_master_vital_bins,
							parallel=True,
							progress_callback=lambda idx, tot, name: emit_progress(
								scheduler.pct("map11", 0.10 + (idx / max(tot, 1)) * 0.20),
								"Map11: Decompile",
								f"({idx}/{tot})",
								f"Map11.wad.client › {name}",
							),
						)

						cleaned_elements = clean_map_elements(
							temp_dir,
							dry_run=dry_run,
							progress_callback=lambda idx, tot, name: emit_progress(
								scheduler.pct("map11", 0.30 + (idx / max(tot, 1)) * 0.25),
								"Map11: Sanitize",
								f"({idx}/{tot})",
								f"Map11.wad.client › {name}",
							),
						)

						generated_chars = process_map11_master_characters(
							temp_dir,
							dry_run=dry_run,
							progress_callback=lambda idx, tot, name: emit_progress(
								scheduler.pct("map11", 0.55 + (idx / max(tot, 1)) * 0.20),
								"Map11: Entities",
								f"({idx}/{tot})",
								f"Map11.wad.client › {name}",
							),
						)

						all_map_recompile = sorted(set(cleaned_elements + generated_chars))
						if all_map_recompile:
							recompile_text_to_bin(
								temp_dir,
								cfg,
								dry_run=dry_run,
								file_selector=lambda _: all_map_recompile,
								parallel=True,
								progress_callback=lambda idx, tot, name: emit_progress(
									scheduler.pct("map11", 0.75 + (idx / max(tot, 1)) * 0.24),
									"Map11: Recompile",
									f"({idx}/{tot})",
									f"Map11.wad.client › {name}",
								),
							)

							if not dry_run:
								shutil.rmtree(map11_cache_dir, ignore_errors=True)
								map11_cache_dir.mkdir(parents=True, exist_ok=True)
								copied = 0
								for txt_file in all_map_recompile:
									out_bin = txt_file.with_name(txt_file.name[:-4])
									if out_bin.is_file():
										rel = out_bin.relative_to(temp_dir)
										dest = map11_cache_dir / rel
										dest.parent.mkdir(parents=True, exist_ok=True)
										shutil.copy2(out_bin, dest)
										copied += 1

								map11_tex_dest = map11_cache_dir / MAP11_TRANSPARENT_TEX_REL
								map11_tex_dest.parent.mkdir(parents=True, exist_ok=True)
								map11_tex_dest.write_bytes(RAW_TRANSPARENT_TEX_BYTES)

								if copied > 0:
									_write_cache_hash(map11_cache_dir, map11_hash, script_hash)

					if opt_structures and not dry_run:
						build_layer_structures(project_dir, map11_cache_dir, dry_run=dry_run)
					if opt_minions and not dry_run:
						build_layer_minions(project_dir, map11_cache_dir, dry_run=dry_run)
					if opt_wards and not dry_run:
						build_layer_wards(project_dir, map11_cache_dir, dry_run=dry_run)
					if opt_accents and not dry_run:
						build_layer_accents(project_dir, map11_cache_dir, dry_run=dry_run)

				emit_progress(scheduler.pct("map11", 1.0), "MAP11 DONE", "", "Map11 processed successfully!")

			# 2. UI.WAD CONTAINER
			needs_ui = opt_finishers and ui_wad and ui_wad.is_file()
			if needs_ui:
				wad_label = "UI"
				emit_progress(scheduler.pct("ui", 0.0), f"PROCESSING {wad_label}", "", f"Checking elements in {ui_wad.name}...")
				ui_hash = file_fingerprint(ui_wad)
				ui_cache_dir = get_canonical_cache_dir(cache_dir, ui_wad)

				if _is_cache_valid(ui_cache_dir, ui_hash, script_hash) and not dry_run:
					LOG.info("Reusing UI elements from master cache: %s", ui_wad.name)
					emit_progress(scheduler.pct("ui", 0.25), f"{wad_label}: Cache", "", f"Valid {ui_wad.name} master cache reused...")
					build_layer_finishers(project_dir, ui_cache_dir, dry_run=dry_run)
				else:
					with tempfile.TemporaryDirectory(dir=str(get_temp_workspace()), prefix="vanilla_finisher_ui_") as temp_fin_str:
						temp_fin = Path(temp_fin_str)
						extract_wad(
							ui_wad,
							temp_fin,
							cfg,
							filter_type="bin",
							filter_pattern=r"(?i)(gameplay\.endofgame\.bin|gameplay\.nexusfinisher\.bin)",
							dry_run=dry_run,
						)

						decompile_bin_files(
							temp_fin,
							cfg,
							dry_run=dry_run,
							parallel=True,
							progress_callback=lambda idx, tot, name: emit_progress(
								scheduler.pct("ui", 0.10 + (idx / max(tot, 1)) * 0.10),
								f"{wad_label}: Decompile",
								f"({idx}/{tot})",
								f"{ui_wad.name} › {name}",
							),
						)

						cleaned_fin = clean_finisher_elements(
							temp_fin,
							dry_run=dry_run,
							progress_callback=lambda idx, tot, name: emit_progress(
								scheduler.pct("ui", 0.25 + (idx / max(tot, 1)) * 0.05),
								f"{wad_label}: Finishers",
								f"({idx}/{tot})",
								f"{ui_wad.name} › {name}",
							),
						)

						if cleaned_fin:
							recompile_text_to_bin(
								temp_fin,
								cfg,
								dry_run=dry_run,
								file_selector=lambda _: cleaned_fin,
								parallel=True,
								progress_callback=lambda idx, tot, name: emit_progress(
									scheduler.pct("ui", 0.35 + (idx / max(tot, 1)) * 0.10),
									f"{wad_label}: Recompile",
									f"({idx}/{tot})",
									f"{ui_wad.name} › {name}",
								),
							)

							if not dry_run:
								shutil.rmtree(ui_cache_dir, ignore_errors=True)
								ui_cache_dir.mkdir(parents=True, exist_ok=True)
								copied = 0
								for cl_txt in cleaned_fin:
									out_b = cl_txt.with_name(cl_txt.name[:-4])
									if out_b.is_file():
										rel = out_b.relative_to(temp_fin)
										dest = ui_cache_dir / rel
										dest.parent.mkdir(parents=True, exist_ok=True)
										shutil.copy2(out_b, dest)
										copied += 1
										dest_fin = project_dir / "content" / "finishers" / ui_wad.name / rel
										dest_fin.parent.mkdir(parents=True, exist_ok=True)
										shutil.copy2(out_b, dest_fin)

								if copied > 0:
									_write_cache_hash(ui_cache_dir, ui_hash, script_hash)

				emit_progress(scheduler.pct("ui", 0.5), f"{wad_label} DONE", "", f"{ui_wad.name} elements processed successfully!")

			# 3. GLOBAL.WAD CONTAINER
			needs_global = (opt_accents or opt_recalls or opt_finishers) and global_wad.is_file()
			if needs_global:
				wad_label = "GLOBAL"
				emit_progress(scheduler.pct("ui", 0.5), f"PROCESSING {wad_label}", "", f"Checking elements in {global_wad.name}...")
				global_hash = file_fingerprint(global_wad)
				global_cache_dir = get_canonical_cache_dir(cache_dir, global_wad)

				if not dry_run:
					build_layer_base(project_dir)

				if _is_cache_valid(global_cache_dir, global_hash, script_hash) and not dry_run:
					LOG.info("Reusing Global elements from master cache: %s", global_wad.name)
					emit_progress(scheduler.pct("ui", 0.75), f"{wad_label}: Cache", "", f"Valid {global_wad.name} master cache reused...")

					for g_file in global_cache_dir.rglob("*.bin"):
						rel_p = g_file.relative_to(global_cache_dir).as_posix().lower()
						if opt_finishers and any(k in rel_p for k in ("nexusfinisher", "endofgame")):
							dest_f = project_dir / "content" / "finishers" / global_wad.name / g_file.relative_to(global_cache_dir)
							dest_f.parent.mkdir(parents=True, exist_ok=True)
							shutil.copy2(g_file, dest_f)
						elif opt_accents and any(k in rel_p for k in ("summonerbanner", "summonertroph")):
							dest_b = project_dir / "content" / "accents" / global_wad.name / g_file.relative_to(global_cache_dir)
							dest_b.parent.mkdir(parents=True, exist_ok=True)
							shutil.copy2(g_file, dest_b)
						elif opt_recalls and "shared" in rel_p:
							dest_r = project_dir / "content" / "recalls" / global_wad.name / g_file.relative_to(global_cache_dir)
							dest_r.parent.mkdir(parents=True, exist_ok=True)
							shutil.copy2(g_file, dest_r)
				else:
					with tempfile.TemporaryDirectory(dir=str(get_temp_workspace()), prefix="vanilla_global_") as temp_glob_str:
						temp_glob = Path(temp_glob_str)
						ext_filter_pattern = r"(?i)(loadouts/nexusfinishers$|loadouts/summonerbanners$|loadouts/summonertrophies$|(?:^|/)shared$)"
						extract_wad(
							global_wad,
							temp_glob,
							cfg,
							filter_type=None,
							filter_pattern=ext_filter_pattern,
							dry_run=dry_run,
						)

						decompile_bin_files(
							temp_glob,
							cfg,
							dry_run=dry_run,
							parallel=True,
							progress_callback=lambda idx, tot, name: emit_progress(
								scheduler.pct("ui", 0.55 + (idx / max(tot, 1)) * 0.10),
								f"{wad_label}: Decompile",
								f"({idx}/{tot})",
								f"{global_wad.name} › {name}",
							),
						)

						cleaned_fin: list[Path] = []
						if opt_finishers:
							cleaned_fin = clean_finisher_elements(
								temp_glob,
								dry_run=dry_run,
								progress_callback=lambda idx, tot, name: emit_progress(
									scheduler.pct("ui", 0.65 + (idx / max(tot, 1)) * 0.05),
									f"{wad_label}: Finishers",
									f"({idx}/{tot})",
									f"{global_wad.name} › {name}",
								),
							)

						cleaned_banners: list[Path] = []
						if opt_accents:
							cleaned_banners = clean_summoner_elements(
								temp_glob,
								dry_run=dry_run,
								progress_callback=lambda idx, tot, name: emit_progress(
									scheduler.pct("ui", 0.70 + (idx / max(tot, 1)) * 0.05),
									f"{wad_label}: Banners",
									f"({idx}/{tot})",
									f"{global_wad.name} › {name}",
								),
							)

						cleaned_recalls: list[Path] = []
						if opt_recalls:
							cleaned_recalls = clean_recall_elements(
								temp_glob,
								workspace_root=workspace_root,
								dry_run=dry_run,
								progress_callback=lambda idx, tot, name: emit_progress(
									scheduler.pct("ui", 0.75 + (idx / max(tot, 1)) * 0.05),
									f"{wad_label}: Recalls",
									f"({idx}/{tot})",
									f"{global_wad.name} › {name}",
								),
							)

						all_glob_recompile = sorted(set(cleaned_banners + cleaned_recalls + cleaned_fin))
						if all_glob_recompile:
							recompile_text_to_bin(
								temp_glob,
								cfg,
								dry_run=dry_run,
								file_selector=lambda _: all_glob_recompile,
								parallel=True,
								progress_callback=lambda idx, tot, name: emit_progress(
									scheduler.pct("ui", 0.85 + (idx / max(tot, 1)) * 0.10),
									f"{wad_label}: Recompile",
									f"({idx}/{tot})",
									f"{global_wad.name} › {name}",
								),
							)

							if not dry_run:
								shutil.rmtree(global_cache_dir, ignore_errors=True)
								global_cache_dir.mkdir(parents=True, exist_ok=True)
								copied = 0
								for cl_txt in all_glob_recompile:
									out_b = cl_txt.with_name(cl_txt.name[:-4])
									if out_b.is_file():
										rel = out_b.relative_to(temp_glob)
										dest = global_cache_dir / rel
										dest.parent.mkdir(parents=True, exist_ok=True)
										shutil.copy2(out_b, dest)
										copied += 1

										rel_p = rel.as_posix().lower()
										if opt_finishers and any(k in rel_p for k in ("nexusfinisher", "endofgame")):
											dest_f = project_dir / "content" / "finishers" / global_wad.name / rel
											dest_f.parent.mkdir(parents=True, exist_ok=True)
											shutil.copy2(out_b, dest_f)
										elif opt_accents and any(k in rel_p for k in ("summonerbanner", "summonertroph")):
											dest_b = project_dir / "content" / "accents" / global_wad.name / rel
											dest_b.parent.mkdir(parents=True, exist_ok=True)
											shutil.copy2(out_b, dest_b)
										elif opt_recalls and "shared" in rel_p:
											dest_r = project_dir / "content" / "recalls" / global_wad.name / rel
											dest_r.parent.mkdir(parents=True, exist_ok=True)
											shutil.copy2(out_b, dest_r)

								global_tex_dest = global_cache_dir / GLOBAL_TRANSPARENT_TEX_REL
								global_tex_dest.parent.mkdir(parents=True, exist_ok=True)
								global_tex_dest.write_bytes(RAW_TRANSPARENT_TEX_BYTES)
								copied += 1

								if copied > 0:
									_write_cache_hash(global_cache_dir, global_hash, script_hash)

				emit_progress(scheduler.pct("ui", 1.0), f"{wad_label} DONE", "", f"{global_wad.name} elements processed successfully!")

			# 4. CHAMPIONS
			if opt_champions and champion_wads:
				emit_progress(scheduler.pct("champions", 0.0), "PROCESSING CHAMPIONS", "", f"Starting reversion of {len(champion_wads)} champions...")
				is_all_cached = not any(
					not _is_cache_valid(get_canonical_cache_dir(cfg["cache_dir"], w), file_fingerprint(w), _script_fingerprint())
					for w in champion_wads
				)
				if is_all_cached and not dry_run:
					emit_progress(scheduler.pct("champions", 0.5), "Champions: Cache", "", "Valid champions master cache reused...")
				
				process_champion_wads(
					champion_wads,
					cfg,
					cfg["cache_dir"],
					dry_run=dry_run,
					progress_callback=lambda ratio, counter, current="": emit_progress(
						scheduler.pct("champions", ratio),
						"Champions: Cache" if is_all_cached else "Champions",
						counter,
						current,
					),
				)

				build_layer_champions(
					champion_wads,
					project_dir,
					cfg["cache_dir"],
					map11_cache_dir=get_canonical_cache_dir(cfg["cache_dir"], map_wad),
					dry_run=dry_run,
				)
				emit_progress(scheduler.pct("champions", 1.0), "CHAMPIONS DONE", "", "All champions processed successfully!")
				
			# 5. PACKAGE
			emit_progress(scheduler.pct("package", 0.0), "PROCESSING PACKAGE", "", "Cataloging assets and assembling MODPKG...")
			output_package = None
			if not dry_run:
				output_package = pack_modpkg_project(
					project_dir,
					workspace_root,
					tools=cfg,
					progress_callback=lambda ratio, phase, stat, curr="": emit_progress(scheduler.pct("package", ratio), "Package", stat, curr),
				)

			emit_progress(100.0, "PACKAGE DONE", "", f"Package completed: {output_package.name if output_package else 'Simulation'}")
			return {
				"output_package": output_package,
				"package_format": package_format,
				"project_dir": project_dir,
				"cfg": cfg,
			}

		# ======================================================================
		# ENGINE 2: LEGACY 1:1 STANDALONE PIPELINE (.FANTOME)
		# ======================================================================
		cache_dir = cfg["cache_dir"]
		allowed_wads = set()

		needs_map = any([opt_accents, opt_wards, opt_structures, opt_minions, opt_finishers, opt_champions]) and map_wad.is_file()
		if needs_map and map_wad.is_file():
			emit_progress(scheduler.pct("map11", 0.0), "PROCESSING MAP11", "", "Verifying canonical Map11 container...")
			wad_hash = file_fingerprint(map_wad)
			script_hash = _script_fingerprint()
			map11_wad_dir = get_canonical_cache_dir(cache_dir, map_wad)

			if _is_cache_valid(map11_wad_dir, wad_hash, script_hash) and not dry_run:
				LOG.info("Reusing Map11 from canonical master cache: %s", map_wad.name)
				emit_progress(scheduler.pct("map11", 0.5), "Map11: Cache", "", "Valid Map11 master cache reused...")
				allowed_wads.add(map_wad.name.lower())
			else:
				with tempfile.TemporaryDirectory(dir=str(get_temp_workspace()), prefix="vanilla_map11_") as temp_dir_str:
					temp_dir = Path(temp_dir_str)
					emit_progress(scheduler.pct("map11", 0.05), "Map11: Extract", "", f"Extracting binaries from {map_wad.name}...")

					extract_wad(
						map_wad,
						temp_dir,
						cfg,
						filter_type="bin",
						dry_run=dry_run,
						progress_callback=lambda status: emit_progress(scheduler.pct("map11", 0.08), "Map11: Extract", "", status),
					)

					decompile_bin_files(
						temp_dir,
						cfg,
						dry_run=dry_run,
						file_selector=_iter_map11_master_vital_bins,
						parallel=True,
						progress_callback=lambda idx, tot, name: emit_progress(
							scheduler.pct("map11", 0.10 + (idx / max(tot, 1)) * 0.20),
							"Map11: Decompile",
							f"({idx}/{tot})",
							f"Map11.wad.client › {name}",
						),
					)

					cleaned_elements = clean_map_elements(
						temp_dir,
						dry_run=dry_run,
						progress_callback=lambda idx, tot, name: emit_progress(
							scheduler.pct("map11", 0.30 + (idx / max(tot, 1)) * 0.25),
							"Map11: Sanitize",
							f"({idx}/{tot})",
							f"Map11.wad.client › {name}",
						),
					)

					generated_chars = process_map11_master_characters(
						temp_dir,
						dry_run=dry_run,
						progress_callback=lambda idx, tot, name: emit_progress(
							scheduler.pct("map11", 0.55 + (idx / max(tot, 1)) * 0.20),
							"Map11: Entities",
							f"({idx}/{tot})",
							f"Map11.wad.client › {name}",
						),
					)

					all_map_recompile = sorted(set(cleaned_elements + generated_chars))
					if all_map_recompile:
						recompile_text_to_bin(
							temp_dir,
							cfg,
							dry_run=dry_run,
							file_selector=lambda _: all_map_recompile,
							parallel=True,
							progress_callback=lambda idx, tot, name: emit_progress(
								scheduler.pct("map11", 0.75 + (idx / max(tot, 1)) * 0.24),
								"Map11: Recompile",
								f"({idx}/{tot})",
								f"Map11.wad.client › {name}",
							),
						)

						if not dry_run:
							shutil.rmtree(map11_wad_dir, ignore_errors=True)
							map11_wad_dir.mkdir(parents=True, exist_ok=True)
							copied = 0
							for txt_file in all_map_recompile:
								out_bin = txt_file.with_name(txt_file.name[:-4])
								if out_bin.is_file():
									rel = out_bin.relative_to(temp_dir)
									dest = map11_wad_dir / rel
									dest.parent.mkdir(parents=True, exist_ok=True)
									shutil.copy2(out_bin, dest)
									copied += 1

							map11_tex_dest = map11_wad_dir / MAP11_TRANSPARENT_TEX_REL
							map11_tex_dest.parent.mkdir(parents=True, exist_ok=True)
							map11_tex_dest.write_bytes(RAW_TRANSPARENT_TEX_BYTES)
							copied += 1

							if copied > 0:
								_write_cache_hash(map11_wad_dir, wad_hash, script_hash)
								allowed_wads.add(map_wad.name.lower())

			emit_progress(scheduler.pct("map11", 1.0), "MAP11 DONE", "", "Map11 processed successfully!")

		needs_ui_or_global = (opt_finishers or opt_accents or opt_recalls)
		candidate_ui_wads: list[Path] = []
		if opt_finishers and ui_wad and ui_wad.is_file():
			candidate_ui_wads.append(ui_wad)
		global_fallback = cfg["final_dir"] / "Global.wad.client"
		if needs_ui_or_global and global_fallback.is_file() and global_fallback not in candidate_ui_wads:
			candidate_ui_wads.append(global_fallback)

		if candidate_ui_wads:
			n_candidates = max(1, len(candidate_ui_wads))
			for c_idx, target_wad in enumerate(candidate_ui_wads):
				is_global = (target_wad.name.lower() == "global.wad.client")
				wad_label = target_wad.name.split('.')[0].upper()
				wad_base_ratio = c_idx / n_candidates
				wad_span_ratio = 1.0 / n_candidates

				emit_progress(
					scheduler.pct("ui", wad_base_ratio),
					f"PROCESSING {wad_label}",
					"",
					f"Checking elements in {target_wad.name}...",
				)

				wad_hash = file_fingerprint(target_wad)
				script_hash = _script_fingerprint()
				wad_cache_dir = get_canonical_cache_dir(cache_dir, target_wad)

				if _is_cache_valid(wad_cache_dir, wad_hash, script_hash) and not dry_run:
					LOG.info("Reusing elements from master cache: %s", target_wad.name)
					emit_progress(
						scheduler.pct("ui", wad_base_ratio + wad_span_ratio * 0.5),
						f"{wad_label}: Cache",
						"",
						f"Valid {target_wad.name} master cache reused...",
					)
					allowed_wads.add(target_wad.name.lower())
				else:
					with tempfile.TemporaryDirectory(dir=str(get_temp_workspace()), prefix="vanilla_finisher_") as temp_fin_str:
						temp_fin = Path(temp_fin_str)

						if is_global:
							ext_filter_pattern = r"(?i)(loadouts/nexusfinishers$|loadouts/summonerbanners$|loadouts/summonertrophies$|(?:^|/)shared$)"
							ext_filter_type = None
						else:
							ext_filter_pattern = r"(?i)(gameplay\.endofgame\.bin|gameplay\.nexusfinisher\.bin)"
							ext_filter_type = "bin"

						extract_wad(
							target_wad,
							temp_fin,
							cfg,
							filter_type=ext_filter_type,
							filter_pattern=ext_filter_pattern,
							dry_run=dry_run,
						)

						def _iter_ui_global_discovered(temp_dir: Path) -> list[Path]:
							vitals: list[Path] = []
							for current, _, filenames in os.walk(str(temp_dir)):
								c_p = Path(current)
								for f in filenames:
									pf = c_p / f
									fl = f.lower()
									if fl.endswith(".bin") or fl.endswith(".materials") or fl.endswith(".ltk.bin") or "." not in f:
										vitals.append(pf)
							return sorted(set(vitals))

						decompile_bin_files(
							temp_fin,
							cfg,
							dry_run=dry_run,
							file_selector=_iter_ui_global_discovered,
							parallel=True,
							progress_callback=lambda idx, tot, name: emit_progress(
								scheduler.pct("ui", wad_base_ratio + wad_span_ratio * (0.2 + (idx / max(tot, 1)) * 0.3)),
								f"{wad_label}: Decompile",
								f"({idx}/{tot})",
								f"{target_wad.name} › {name}",
							),
						)

						cleaned_fin = clean_finisher_elements(
							temp_fin,
							dry_run=dry_run,
							progress_callback=lambda idx, tot, name: emit_progress(
								scheduler.pct("ui", wad_base_ratio + wad_span_ratio * (0.5 + (idx / max(tot, 1)) * 0.1)),
								f"{wad_label}: Finishers",
								f"({idx}/{tot})",
								f"{target_wad.name} › {name}",
							),
						)

						cleaned_banners: list[Path] = []
						if is_global:
							cleaned_banners = clean_summoner_elements(
								temp_fin,
								dry_run=dry_run,
								progress_callback=lambda idx, tot, name: emit_progress(
									scheduler.pct("ui", wad_base_ratio + wad_span_ratio * (0.6 + (idx / max(tot, 1)) * 0.05)),
									f"{wad_label}: Banners",
									f"({idx}/{tot})",
									f"{target_wad.name} › {name}",
								),
							)

						cleaned_recalls: list[Path] = []
						if is_global:
							cleaned_recalls = clean_recall_elements(
								temp_fin,
								workspace_root=workspace_root,
								dry_run=dry_run,
								progress_callback=lambda idx, tot, name: emit_progress(
									scheduler.pct("ui", wad_base_ratio + wad_span_ratio * (0.65 + (idx / max(tot, 1)) * 0.05)),
									f"{wad_label}: Recalls",
									f"({idx}/{tot})",
									f"{target_wad.name} › {name}",
								),
							)

						all_ui_global_recompile = sorted(set(cleaned_fin + cleaned_banners + cleaned_recalls))
						if all_ui_global_recompile:
							recompile_text_to_bin(
								temp_fin,
								cfg,
								dry_run=dry_run,
								file_selector=lambda _: all_ui_global_recompile,
								parallel=True,
								progress_callback=lambda idx, tot, name: emit_progress(
									scheduler.pct("ui", wad_base_ratio + wad_span_ratio * (0.7 + (idx / max(tot, 1)) * 0.28)),
									f"{wad_label}: Recompile",
									f"({idx}/{tot})",
									f"{target_wad.name} › {name}",
								),
							)

							if not dry_run:
								shutil.rmtree(wad_cache_dir, ignore_errors=True)
								wad_cache_dir.mkdir(parents=True, exist_ok=True)
								copied = 0
								for clean_txt in all_ui_global_recompile:
									out_bin = clean_txt.with_name(clean_txt.name[:-4])
									if out_bin.is_file():
										rel = out_bin.relative_to(temp_fin)
										dest = wad_cache_dir / rel
										dest.parent.mkdir(parents=True, exist_ok=True)
										shutil.copy2(out_bin, dest)
										copied += 1

								if is_global:
									global_tex_dest = wad_cache_dir / GLOBAL_TRANSPARENT_TEX_REL
									global_tex_dest.parent.mkdir(parents=True, exist_ok=True)
									global_tex_dest.write_bytes(RAW_TRANSPARENT_TEX_BYTES)
									copied += 1

								if copied > 0:
									_write_cache_hash(wad_cache_dir, wad_hash, script_hash)
									allowed_wads.add(target_wad.name.lower())

				emit_progress(
					scheduler.pct("ui", wad_base_ratio + wad_span_ratio),
					f"{wad_label} DONE",
					"",
					f"{target_wad.name} elements processed successfully!",
				)

		if opt_champions:
			emit_progress(scheduler.pct("champions", 0.0), "PROCESSING CHAMPIONS", "", f"Starting reversion of {len(champion_wads)} champions...")
			is_all_cached = not any(
				not _is_cache_valid(get_canonical_cache_dir(cfg["cache_dir"], w), file_fingerprint(w), _script_fingerprint())
				for w in champion_wads
			)
			if is_all_cached and not dry_run:
				emit_progress(scheduler.pct("champions", 0.5), "Champions: Cache", "", "Valid champions master cache reused...")
			process_champion_wads(
				champion_wads,
				cfg,
				cfg["cache_dir"],
				dry_run=dry_run,
				progress_callback=lambda ratio, counter, current="": emit_progress(
					scheduler.pct("champions", ratio),
					"Champions: Cache" if is_all_cached else "Champions",
					counter,
					current,
				),
			)
			allowed_wads.update({w.name.lower() for w in champion_wads})
			emit_progress(scheduler.pct("champions", 1.0), "CHAMPIONS DONE", "", "All champions processed successfully!")

		ghost_path = workspace_root / "VanillaTrueMod.fantome"
		emit_progress(scheduler.pct("package", 0.0), "PROCESSING PACKAGE", "", "Cataloging assets and assembling standalone FANTOME...")
		
		fantome_output = None
		if not dry_run:
			fantome_output = create_standalone_fantome(
				cfg["cache_dir"],
				ghost_path,
				allowed_wads=allowed_wads,
				options=options,
				dry_run=False,
				progress_callback=lambda ratio, phase, stat, curr="": emit_progress(
					scheduler.pct("package", ratio),
					"Package",
					stat,
					curr,
				),
			)
		emit_progress(100.0, "PACKAGE DONE", "", f"Package completed: {fantome_output.name if fantome_output else 'Simulation'}")

		return {
			"output_package": fantome_output,
			"package_format": package_format,
			"ghost_path": ghost_path,
			"allowed_wads": allowed_wads,
			"options": options,
			"cfg": cfg,
			"scheduler": scheduler,
		}
	finally:
		cleanup_temp_dirs()


# ==============================================================================
# ENTRY POINT & HEXTECH GRAPHICAL INTERFACE
# ==============================================================================
def main() -> None:
	default_paths = resolve_default_paths()
	parser = argparse.ArgumentParser(description="Vanilla True Builder - Suite")
	parser.add_argument("--map-wad", default=str(default_paths["map_wad"]), help="Path to Map11.wad.client")
	parser.add_argument("--ui-wad", default=str(default_paths["ui_wad"]) if default_paths["ui_wad"] else None, help="Path to UI.wad.client")
	parser.add_argument("--workspace", default=str(default_paths["workspace_root"]), help="Workspace root directory")
	parser.add_argument("--gui", action="store_true", help="Launch graphical user interface")
	parser.add_argument("--dry-run", action="store_true", help="Run simulation without writing files")
	parser.add_argument("--update-hashes", action="store_true", help="Force update of CommunityDragon hashes")
	parser.add_argument("--format", choices=["modpkg", "fantome"], default="modpkg", help="Packaging format (modpkg or fantome)")
	parser.add_argument("--no-accents", action="store_true", help="Do not omit map accents, statues, banners and trophies")
	parser.add_argument("--no-wards", action="store_true", help="Do not revert vision wards")
	parser.add_argument("--no-structures", action="store_true", help="Do not revert structures (Turrets, Rubble, Inhibitors, Nexus, Shopkeepers)")
	parser.add_argument("--no-minions", action="store_true", help="Do not revert minions and creeps")
	parser.add_argument("--no-finishers", action="store_true", help="Do not neutralize nexus finishers")
	parser.add_argument("--no-recalls", action="store_true", help="Do not neutralize special recall overlays (Honor, Challenger)")
	parser.add_argument("--no-champions", action="store_true", help="Do not revert champion skins")
	args = parser.parse_args()

	map_wad = Path(args.map_wad).expanduser()
	ui_wad = Path(args.ui_wad).expanduser() if args.ui_wad else default_paths["ui_wad"]
	workspace_root = Path(args.workspace).expanduser()

	if args.update_hashes:
		ensure_ritobin_hashes(workspace_root / "tools" / "hashes", force=True)

	if args.gui or len(sys.argv) == 1:
		if tk is None:
			LOG.error("Tkinter is not available in the current Python environment.")
			raise SystemExit(1)
		app = TkApp(map_wad, workspace_root, ui_wad=ui_wad, dry_run=args.dry_run)
		app.run()
		return

	if not map_wad.exists():
		LOG.error("Map WAD not found: %s", map_wad)
		raise SystemExit(1)

	cfg = setup_workspace(workspace_root)
	options = {
		"accents": not args.no_accents,
		"wards": not args.no_wards,
		"structures": not args.no_structures,
		"minions": not args.no_minions,
		"finishers": not args.no_finishers,
		"recalls": not args.no_recalls,
		"champions": not args.no_champions,
	}

	result = build_vanilla_pipeline(
		map_wad,
		workspace_root,
		ui_wad=ui_wad,
		options=options,
		dry_run=args.dry_run,
		cfg=cfg,
		package_format=args.format,
	)

	if not args.dry_run and result.get("output_package"):
		LOG.info("Package successfully generated: %s", result["output_package"])
	cleanup_temp_dirs()


class TkApp:
	C_TITLEBAR = "#0b1016"
	C_TITLEBAR_BORDER = "#1a2530"
	C_TITLEBAR_TEXT = "#ffffff"
	C_TITLEBAR_ICONS = "#7d8b99"
	C_TITLEBAR_HOVER = "#18202d"
	C_CLOSE_HOVER = "#c42b1c"

	C_BG_ROOT = "#080c12"
	C_BG_CARD = "#0f1823"
	C_BG_CARD_HOVER = "#162334"
	C_BG_TERMINAL = "#03060a"
	C_BORDER_GOLD = "#785a28"
	C_BORDER_DARK = "#1a2530"
	C_GOLD_TEXT = "#f0e6d2"
	C_GOLD_ACCENT = "#c8aa6e"
	C_CYAN_MAGIC = "#0ac8b9"
	C_TEXT_MUTED = "#7a8288"
	C_TEXT_TERMINAL = "#cdbe91"
	C_RED_ERROR = "#ff4655"
	C_YELLOW_ACTIVE = "#EBEB5F"
	C_GREEN_SUCCESS = "#3cd070"

	def __init__(self, map_wad: Path, workspace_root: Path, ui_wad: Path | None = None, dry_run: bool = False):
		self.map_wad = map_wad
		self.ui_wad = ui_wad
		self.workspace_root = workspace_root
		self.dry_run = dry_run
		self.queue: queue.Queue = queue.Queue()

		self.root = tk.Tk()
		self.root.title("Vanilla True Builder")
		self.root.geometry("880x790+250+80")
		self.root.minsize(820, 700)
		self.root.configure(bg=self.C_BG_ROOT)

		self.root.overrideredirect(True)
		self.is_maximized = False
		self._drag_data = {"x": 0, "y": 0}

		self._integrate_windows_taskbar()

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

		self.status_var = tk.StringVar(value="0.0%")
		self.percent_var = tk.DoubleVar(value=0.0)
		self.max_percent = 0.0
		self.last_logged_phase = ""
		self.phase_var = tk.StringVar(value="IDLE")
		self.counter_var = tk.StringVar(value="")
		self.current_file_var = tk.StringVar(value="Waiting for command...")
		self.launcher_status_var = tk.StringVar(value="SYNCING...")

		self.pkg_format_var = tk.StringVar(value="modpkg")

		self.chk_accents = tk.BooleanVar(value=True)
		self.chk_wards = tk.BooleanVar(value=True)
		self.chk_structures = tk.BooleanVar(value=True)
		self.chk_minions = tk.BooleanVar(value=True)
		self.chk_finishers = tk.BooleanVar(value=True)
		self.chk_recalls = tk.BooleanVar(value=True)
		self.chk_champions = tk.BooleanVar(value=True)

		self._configure_styles()
		self._build_custom_titlebar()
		self._build_ui()
		self.error_log_path: Path = self.workspace_root / "VanillaTrue_error.log"

		threading.Thread(target=self._check_initial_launcher_status, daemon=True).start()
		threading.Thread(target=self._check_github_app_update, daemon=True).start()

	def _integrate_windows_taskbar(self) -> None:
		if sys.platform == "win32":
			try:
				import ctypes
				ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("neve.vanillabuilder.main.1")
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

		title = tk.Label(left_frame, text="Vanilla True Builder", bg=self.C_TITLEBAR, fg=self.C_TITLEBAR_TEXT, font=("Segoe UI", 9, "bold"))
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
				ctypes.windll.user32.ShowWindow(self.hwnd, 6)
				return
			except Exception:
				pass
		self.root.iconify()

	def _configure_styles(self) -> None:
		style = ttk.Style(self.root)
		try:
			style.theme_use("clam")
		except Exception:
			pass

		style.configure("Root.TFrame", background=self.C_BG_ROOT)
		style.configure("Card.TFrame", background=self.C_BG_CARD, relief="solid", borderwidth=1, bordercolor=self.C_BORDER_GOLD)
		style.configure("CardInner.TFrame", background=self.C_BG_CARD, borderwidth=0)

		style.configure("Title.TLabel", background=self.C_BG_ROOT, foreground=self.C_GOLD_ACCENT, font=("Segoe UI", 15, "bold"))
		style.configure("Credit.TLabel", background=self.C_BG_ROOT, foreground="#586675", font=("Segoe UI", 8))
		style.configure("Status.TLabel", background=self.C_BG_ROOT, foreground=self.C_CYAN_MAGIC, font=("Segoe UI", 13, "bold"))
		style.configure("Phase.TLabel", background=self.C_BG_ROOT, foreground=self.C_GOLD_ACCENT, font=("Segoe UI", 9, "bold"))
		style.configure("Counter.TLabel", background=self.C_BG_ROOT, foreground=self.C_CYAN_MAGIC, font=("Segoe UI", 9, "bold"))
		style.configure("File.TLabel", background=self.C_BG_ROOT, foreground=self.C_TEXT_MUTED, font=("Segoe UI", 8))

		style.configure(
			"Hextech.TCheckbutton",
			background=self.C_BG_CARD,
			foreground=self.C_GOLD_TEXT,
			font=("Segoe UI", 9),
			focuscolor=self.C_BG_CARD,
			indicatorrelief="flat",
			padding=2,
		)
		style.map(
			"Hextech.TCheckbutton",
			background=[("active", self.C_BG_CARD_HOVER), ("disabled", self.C_BG_CARD)],
			foreground=[("disabled", "#4e5a67"), ("active", self.C_GOLD_ACCENT)],
			indicatorcolor=[
				("disabled", "#141c24"),
				("selected", self.C_CYAN_MAGIC),
				("pressed", self.C_GOLD_ACCENT),
			],
		)

		style.configure(
			"Hextech.TRadiobutton",
			background=self.C_BG_CARD,
			foreground=self.C_GOLD_TEXT,
			font=("Segoe UI", 9),
			focuscolor=self.C_BG_CARD,
			indicatorrelief="flat",
			padding=2,
		)
		style.map(
			"Hextech.TRadiobutton",
			background=[("active", self.C_BG_CARD_HOVER)],
			foreground=[("active", self.C_GOLD_ACCENT)],
			indicatorcolor=[("selected", self.C_CYAN_MAGIC), ("pressed", self.C_GOLD_ACCENT)],
		)

		style.configure(
			"HextechAction.TButton",
			background="#16202c",
			foreground=self.C_GOLD_ACCENT,
			font=("Segoe UI", 10, "bold"),
			borderwidth=1,
			bordercolor=self.C_BORDER_GOLD,
			relief="solid",
		)
		style.map(
			"HextechAction.TButton",
			background=[("active", "#253344"), ("disabled", "#0b1016")],
			foreground=[("active", self.C_GOLD_TEXT), ("disabled", "#444444")],
			bordercolor=[("active", self.C_CYAN_MAGIC), ("disabled", "#1a2530")],
		)

		style.configure(
			"HextechUtil.TButton",
			background=self.C_BG_ROOT,
			foreground=self.C_GOLD_ACCENT,
			font=("Segoe UI", 8, "bold"),
			borderwidth=1,
			bordercolor=self.C_BORDER_DARK,
			relief="solid",
		)
		style.map(
			"HextechUtil.TButton",
			background=[("disabled", "#0b1016"), ("active", self.C_BG_CARD), ("pressed", "#1f2c3a")],
			foreground=[("disabled", "#4e5a67"), ("active", self.C_GOLD_TEXT)],
			bordercolor=[("disabled", "#141c24"), ("active", self.C_BORDER_GOLD)],
		)

		style.configure(
			"Hextech.Horizontal.TProgressbar",
			troughcolor=self.C_BG_CARD,
			background=self.C_CYAN_MAGIC,
			lightcolor=self.C_CYAN_MAGIC,
			darkcolor=self.C_CYAN_MAGIC,
			bordercolor=self.C_BORDER_DARK,
			thickness=6,
		)

	def launch_auditor(self) -> None:
		kwargs: dict = {}
		if sys.platform == "win32":
			startupinfo = subprocess.STARTUPINFO()
			startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
			startupinfo.wShowWindow = subprocess.SW_HIDE
			kwargs["startupinfo"] = startupinfo

		try:
			if getattr(sys, "frozen", False):
				subprocess.Popen([sys.executable, "--auditor"], cwd=str(self.workspace_root), **kwargs)
			else:
				script = self.workspace_root / "auditor.py"
				subprocess.Popen([sys.executable, str(script)], cwd=str(self.workspace_root), **kwargs)
		except Exception as exc:
			messagebox.showerror("Launch Error", f"Failed to launch auditor:\n{exc}")

	def _on_format_changed(self) -> None:
		fmt = self.pkg_format_var.get().lower()
		self.btn_compile.config(text=f"BUILD PACKAGE (.{fmt.upper()})")

		if fmt == "modpkg":
			if hasattr(self, "fantome_modules_container") and self.fantome_modules_container.winfo_ismapped():
				self.fantome_modules_container.pack_forget()
		else:
			if hasattr(self, "fantome_modules_container") and not self.fantome_modules_container.winfo_ismapped():
				self.fantome_modules_container.pack(fill=tk.X, pady=(0, 10), before=self.action_frame)

	def _select_format(self, val: str) -> None:
		if hasattr(self, "btn_compile") and str(self.btn_compile["state"]) == "disabled":
			return
		if self.pkg_format_var.get().lower() == val:
			return
		self.pkg_format_var.set(val)
		self._update_format_switcher()
		self._on_format_changed()

	def _hover_format_tab(self, val: str, is_hover: bool) -> None:
		if hasattr(self, "btn_compile") and str(self.btn_compile["state"]) == "disabled":
			return
		if self.pkg_format_var.get().lower() == val:
			return
		d = self.switcher_tabs.get(val)
		if not d:
			return
		bg = "#111a24" if is_hover else "#0a1017"
		d["frame"].config(bg=bg)
		d["center"].config(bg=bg)
		d["icon"].config(bg=bg)
		d["title"].config(bg=bg)

	def _update_format_switcher(self) -> None:
		cur = self.pkg_format_var.get().lower()
		for val, d in self.switcher_tabs.items():
			info = self.switcher_info[val]
			is_act = (val == cur)

			bg = info["bg_active"] if is_act else "#0a1017"
			border = info["col"] if is_act else "#0a1017"
			icon_char = "◆" if is_act else "◇"
			icon_fg = info["col"] if is_act else self.C_TEXT_MUTED
			title_fg = self.C_GOLD_TEXT if is_act else self.C_TEXT_MUTED
			pill_bg = info["pill_bg_active"] if is_act else "#080c11"
			pill_fg = info["col"] if is_act else "#4e5a67"

			d["frame"].config(bg=bg, highlightthickness=1, highlightbackground=border)
			d["center"].config(bg=bg)
			d["icon"].config(text=icon_char, fg=icon_fg, bg=bg)
			d["title"].config(fg=title_fg, bg=bg)
			d["badge"].config(fg=pill_fg, bg=pill_bg)

		if hasattr(self, "format_info_label") and cur in self.switcher_info:
			self.format_info_label.config(text=f"ⓘ  {self.switcher_info[cur]['tip']}")

	def _build_ui(self) -> None:
		root_frame = ttk.Frame(self.root, style="Root.TFrame", padding=(16, 10, 16, 12))
		root_frame.pack(fill=tk.BOTH, expand=True)

		header = ttk.Frame(root_frame, style="Root.TFrame")
		header.pack(fill=tk.X, pady=(0, 6))

		header_left = ttk.Frame(header, style="Root.TFrame")
		header_left.pack(side=tk.LEFT, anchor="w")

		ttk.Label(header_left, text="Status", style="Title.TLabel").pack(anchor="w")

		self.launcher_status_label = tk.Label(
			header_left,
			textvariable=self.launcher_status_var,
			bg=self.C_BG_ROOT,
			fg=self.C_GOLD_ACCENT,
			font=("Segoe UI", 8, "bold"),
		)
		self.launcher_status_label.pack(anchor="w", pady=(2, 0))

		header_right = ttk.Frame(header, style="Root.TFrame")
		header_right.pack(side=tk.RIGHT, anchor="ne")
		ttk.Label(header_right, text="by Neve | RAKUZAN", style="Credit.TLabel").pack(anchor="e")

		format_row = ttk.Frame(root_frame, style="Root.TFrame")
		format_row.pack(fill=tk.X, pady=(0, 0))

		ttk.Label(format_row, text="PACKAGE FORMAT", style="Phase.TLabel").pack(side=tk.LEFT, anchor="sw", pady=(0, 4))

		tools_frame = ttk.Frame(format_row, style="Root.TFrame")
		tools_frame.pack(side=tk.RIGHT, anchor="se", pady=(0, 12))
		self.btn_skin_fix = ttk.Button(tools_frame, text="SKIN FIX", style="HextechUtil.TButton", command=self.launch_auditor)
		self.btn_skin_fix.pack(side=tk.LEFT)

		format_box = tk.Frame(root_frame, bg=self.C_BG_ROOT)
		format_box.pack(fill=tk.X, pady=(0, 10))

		switcher = tk.Frame(format_box, bg="#0a1017", highlightthickness=1, highlightbackground=self.C_BORDER_DARK, padx=3, pady=3)
		switcher.pack(fill=tk.X)

		self.switcher_info = {
			"modpkg": {
				"title": ".MODPKG",
				"badge": "RECOMMENDED",
				"tip": "All-in-one package, turn individual features on or off directly inside LTK Manager",
				"col": self.C_CYAN_MAGIC,
				"bg_active": "#12202e",
				"pill_bg_active": "#162f42",
			},
			"fantome": {
				"title": ".FANTOME",
				"badge": "LEGACY",
				"tip": "Traditional standalone archive with pre-compiled fixed modules",
				"col": self.C_GOLD_ACCENT,
				"bg_active": "#1f1910",
				"pill_bg_active": "#2b2214",
			},
		}

		self.switcher_tabs = {}

		self.format_info_label = tk.Label(format_box, text="", bg=self.C_BG_ROOT, fg=self.C_GOLD_TEXT, font=("Segoe UI", 8, "bold"))
		self.format_info_label.pack(anchor="w", padx=2, pady=(4, 0))

		for i, (val, info) in enumerate(self.switcher_info.items()):
			tab_frame = tk.Frame(switcher, bg="#0a1017", padx=8, pady=8)
			tab_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0 if i == 0 else 4, 0))

			center_box = tk.Frame(tab_frame, bg="#0a1017")
			center_box.pack(anchor="center")

			lbl_icon = tk.Label(center_box, text="◇", bg="#0a1017", fg=self.C_TEXT_MUTED, font=("Segoe UI", 9, "bold"))
			lbl_icon.pack(side=tk.LEFT, padx=(0, 6))

			lbl_title = tk.Label(center_box, text=info["title"], bg="#0a1017", fg=self.C_TEXT_MUTED, font=("Segoe UI", 9, "bold"))
			lbl_title.pack(side=tk.LEFT, padx=(0, 6))

			lbl_badge = tk.Label(
				center_box,
				text=info["badge"],
				bg="#080c11",
				fg="#4e5a67",
				font=("Segoe UI", 7, "bold"),
				padx=5,
				pady=1,
			)
			lbl_badge.pack(side=tk.LEFT)

			self.switcher_tabs[val] = {
				"frame": tab_frame,
				"center": center_box,
				"icon": lbl_icon,
				"title": lbl_title,
				"badge": lbl_badge,
			}

			for w in (tab_frame, center_box, lbl_icon, lbl_title, lbl_badge):
				w.bind("<Button-1>", lambda e, v=val: self._select_format(v))
				w.bind("<Enter>", lambda e, v=val: self._hover_format_tab(v, True))
				w.bind("<Leave>", lambda e, v=val: self._hover_format_tab(v, False))

		self._update_format_switcher()

		footer_frame = ttk.Frame(root_frame, style="Root.TFrame")
		footer_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(12, 0))

		self.btn_open_folder = ttk.Button(
			footer_frame,
			text="📁 OPEN BUILD FOLDER",
			style="HextechUtil.TButton",
			command=self.open_output_folder,
		)
		self.btn_open_folder.pack(side=tk.RIGHT)

		self.error_btn = ttk.Button(
			footer_frame,
			text="⚠ OPEN DETAILED ERROR REPORT (LOG)",
			style="HextechUtil.TButton",
			command=self.open_error_log,
		)
		self.error_btn.pack(side=tk.LEFT)
		self.error_btn.pack_forget()

		self.action_frame = ttk.Frame(root_frame, style="Root.TFrame")

		self.fantome_modules_container = ttk.Frame(root_frame, style="Root.TFrame")

		modules_row = ttk.Frame(self.fantome_modules_container, style="Root.TFrame")
		modules_row.pack(fill=tk.X, pady=(0, 0))

		ttk.Label(modules_row, text="MODULES", style="Phase.TLabel").pack(side=tk.LEFT, anchor="sw", pady=(0, 2))

		modules_card = ttk.Frame(self.fantome_modules_container, style="Card.TFrame", padding=(12, 6, 12, 6))
		modules_card.pack(fill=tk.X, pady=(0, 0))

		card_content = ttk.Frame(modules_card, style="CardInner.TFrame")
		card_content.pack(fill=tk.X)

		self.module_defs = [
			("Disable Map Accents", "Remove all event assets and clash banners", self.chk_accents),
			("Disable Ward Skins", "Set all ward skins to default", self.chk_wards),
			("Disable Structures Skins", "Restore all turrets, rubble, inhibitors, nexus and shopkeepers to default", self.chk_structures),
			("Disable Minions Skins", "Restore all minions theme to default", self.chk_minions),
			("Disable Nexus Finishers", "Set all nexus finishers skins to default", self.chk_finishers),
			("Disable Special Recalls", "Restore all special recalls to default", self.chk_recalls),
			("Disable Champions Skins", "Set all champions skins to default", self.chk_champions),
		]

		self.module_checkboxes = []
		for title, desc, var in self.module_defs:
			chk = ttk.Checkbutton(card_content, text=f"{title} — {desc}", variable=var, style="Hextech.TCheckbutton")
			chk.pack(anchor="w", pady=1)
			self.module_checkboxes.append(chk)

		ctrl_row = ttk.Frame(modules_card, style="CardInner.TFrame")
		ctrl_row.pack(fill=tk.X, pady=(6, 2))
		self.btn_select_all = ttk.Button(ctrl_row, text="Select All", style="HextechUtil.TButton", command=self.select_all)
		self.btn_select_all.pack(side=tk.LEFT, padx=(0, 6))
		self.btn_deselect_all = ttk.Button(ctrl_row, text="Deselect All", style="HextechUtil.TButton", command=self.deselect_all)
		self.btn_deselect_all.pack(side=tk.LEFT)

		self.action_frame.pack(fill=tk.X, pady=(0, 10))

		self.btn_compile = ttk.Button(
			self.action_frame,
			text="BUILD PACKAGE (.MODPKG)",
			style="HextechAction.TButton",
			command=self.start_build,
		)
		self.btn_compile.pack(fill=tk.X, ipady=6)

		prog_frame = ttk.Frame(root_frame, style="Root.TFrame")
		prog_frame.pack(fill=tk.X, pady=(0, 4))

		self.progress = ttk.Progressbar(
			prog_frame,
			orient=tk.HORIZONTAL,
			mode="determinate",
			variable=self.percent_var,
			style="Hextech.Horizontal.TProgressbar",
		)
		self.progress.pack(fill=tk.X, pady=(0, 4))

		info_row = ttk.Frame(prog_frame, style="Root.TFrame")
		info_row.pack(fill=tk.X)

		self.percent_label = ttk.Label(info_row, textvariable=self.status_var, style="Status.TLabel")
		self.percent_label.pack(side=tk.LEFT)

		self.phase_label = ttk.Label(info_row, textvariable=self.phase_var, style="Phase.TLabel")
		self.phase_label.pack(side=tk.LEFT, padx=(12, 6))

		self.counter_label = ttk.Label(info_row, textvariable=self.counter_var, style="Counter.TLabel")
		self.counter_label.pack(side=tk.LEFT)

		self.current_file_label = ttk.Label(info_row, textvariable=self.current_file_var, style="File.TLabel")
		self.current_file_label.pack(side=tk.RIGHT)

		console_card = ttk.Frame(root_frame, style="Card.TFrame", padding=2)
		console_card.pack(fill=tk.BOTH, expand=True)

		self.trace = tk.Text(
			console_card,
			height=5,
			wrap=tk.WORD,
			state=tk.DISABLED,
			bg=self.C_BG_TERMINAL,
			fg=self.C_TEXT_TERMINAL,
			insertbackground=self.C_CYAN_MAGIC,
			selectbackground=self.C_BORDER_GOLD,
			relief=tk.FLAT,
			font=("Consolas", 9),
		)
		self.trace.tag_configure("gold", foreground=self.C_GOLD_ACCENT)
		self.trace.tag_configure("cyan", foreground=self.C_CYAN_MAGIC)
		self.trace.tag_configure("green", foreground=self.C_GREEN_SUCCESS)
		self.trace.tag_configure("red", foreground=self.C_RED_ERROR)
		self.trace.tag_configure("white", foreground="#ffffff")

		trace_scrollbar = ttk.Scrollbar(console_card, orient=tk.VERTICAL, command=self.trace.yview)
		self.trace.configure(yscrollcommand=trace_scrollbar.set)
		self.trace.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
		trace_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

		self._append_trace(" VANILLA TRUE BUILDER — Ready for compilation.", tag="cyan")

	def select_all(self) -> None:
		if hasattr(self, "btn_compile") and str(self.btn_compile["state"]) == "disabled":
			return
		for _, _, var in self.module_defs:
			var.set(True)

	def deselect_all(self) -> None:
		if hasattr(self, "btn_compile") and str(self.btn_compile["state"]) == "disabled":
			return
		for _, _, var in self.module_defs:
			var.set(False)

	def open_output_folder(self) -> None:
		target = self.workspace_root
		try:
			if sys.platform == "win32":
				os.startfile(str(target))
			else:
				subprocess.Popen(["xdg-open", str(target)])
		except Exception as e:
			messagebox.showerror("Error", f"Could not open output folder: {e}")

	def _set_status_badge(self, text: str, color: str) -> None:
		self.launcher_status_var.set(text)
		self.launcher_status_label.config(fg=color)

	def _check_initial_launcher_status(self) -> None:
		try:
			cache_dir = self.workspace_root / "cache"
			modpkg = self.workspace_root / "VanillaTrueMod.modpkg"
			fantome = self.workspace_root / "VanillaTrueMod.fantome"

			if not modpkg.is_file() and not fantome.is_file():
				self.root.after(0, lambda: self._set_status_badge("READY TO BUILD", self.C_CYAN_MAGIC))
				return

			if not cache_dir.is_dir():
				self.root.after(0, lambda: self._set_status_badge("READY TO BUILD", self.C_CYAN_MAGIC))
				return

			outdated = 0
			valid_cached_count = 0
			script_hash = _script_fingerprint()

			if self.map_wad.is_file():
				map_cache = get_canonical_cache_dir(cache_dir, self.map_wad)
				saved_wad, saved_script = _read_cache_hashes(map_cache)
				if saved_wad:
					valid_cached_count += 1
					if saved_wad != file_fingerprint(self.map_wad) or not (saved_script and saved_script.startswith(script_hash)):
						outdated += 1

			global_wad = DEFAULT_FINAL_DIR / "Global.wad.client"
			if global_wad.is_file():
				glob_cache = get_canonical_cache_dir(cache_dir, global_wad)
				saved_wad, saved_script = _read_cache_hashes(glob_cache)
				if saved_wad:
					valid_cached_count += 1
					if saved_wad != file_fingerprint(global_wad) or saved_script != script_hash:
						outdated += 1

			if outdated == 0:
				for wad_entry in cache_dir.iterdir():
					if not wad_entry.is_dir() or not wad_entry.name.lower().endswith(".wad.client"):
						continue
					if wad_entry.name.lower() in (self.map_wad.name.lower(), "global.wad.client", "ui.wad.client"):
						continue
					saved_wad, saved_script = _read_cache_hashes(wad_entry)
					if saved_wad:
						valid_cached_count += 1
						champ_game_wad = DEFAULT_CHAMPIONS_DIR / wad_entry.name
						if champ_game_wad.is_file():
							if saved_wad != file_fingerprint(champ_game_wad) or saved_script != script_hash:
								outdated += 1
								break

			if valid_cached_count == 0:
				self.root.after(0, lambda: self._set_status_badge("READY TO BUILD", self.C_CYAN_MAGIC))
			elif outdated > 0:
				self.root.after(0, lambda: self._set_status_badge("MOD UPDATE REQUIRED", self.C_RED_ERROR))
			else:
				self.root.after(0, lambda: self._set_status_badge("MOD UP TO DATE", self.C_GREEN_SUCCESS))
		except Exception:
			self.root.after(0, lambda: self._set_status_badge("MOD READY", self.C_GOLD_ACCENT))

	def _set_buttons_state(self, state: str) -> None:
		self.btn_compile.config(state=state)
		if hasattr(self, "btn_select_all"):
			self.btn_select_all.config(state=state)
		if hasattr(self, "btn_deselect_all"):
			self.btn_deselect_all.config(state=state)
		for chk in getattr(self, "module_checkboxes", []):
			chk.config(state=state)

	def _append_trace(self, message: str, tag: str | None = None) -> None:
		self.trace.configure(state=tk.NORMAL)
		if tag:
			self.trace.insert(tk.END, message + "\n", tag)
		else:
			self.trace.insert(tk.END, message + "\n")
		line_count = int(self.trace.index("end-1c").split(".")[0])
		if line_count > 1500:
			self.trace.delete("1.0", "300.0")
		self.trace.see(tk.END)
		self.trace.configure(state=tk.DISABLED)

	def run(self) -> None:
		self.poll_queue()
		self.root.mainloop()

	def poll_queue(self) -> None:
		try:
			while True:
				item = self.queue.get_nowait()
				typ = item.get("type")
				if typ == "progress":
					pct = float(item.get("percent", 0.0))
					self.max_percent = max(self.max_percent, pct)
					pct = self.max_percent
					status = item.get("status", "")
					phase = item.get("phase", "PROCESSING")
					current = item.get("current_file", "")

					clean_phase = phase.replace("[", "").replace("]", "").strip().upper()

					counter_match = re.search(r'[\(\[]?(\d+\s*/\s*\d+)[\)\]]?', status)
					if not counter_match and current:
						counter_match = re.search(r'[\(\[]?(\d+\s*/\s*\d+)[\)\]]?', current)
					counter_str = f"[{counter_match.group(1)}]" if counter_match else ""

					self.percent_var.set(pct)
					self.status_var.set(f"{pct:.1f}%")
					self.phase_var.set(clean_phase)
					self.counter_var.set(counter_str)
					self.current_file_var.set(current if current else status)
					pct_str = f"[{pct:.1f}%]"
					phase_upper = clean_phase.upper()

					if clean_phase and clean_phase != self.last_logged_phase:
						if "DONE" in phase_upper or "COMPLETE" in phase_upper:
							self._append_trace(f"{pct_str} ✔ {clean_phase}!", tag="green")
						elif "PROCESSING" in phase_upper:
							self._append_trace(f"{pct_str} ✦ {clean_phase}...", tag="gold")
						else:
							self._append_trace(f"{pct_str}   • {clean_phase}", tag="white")
						self.last_logged_phase = clean_phase
				elif typ == "log":
					self._append_trace(str(item.get("message", "")), tag=item.get("tag"))
				elif typ == "done":
					self.percent_var.set(100.0)
					self.status_var.set("100%")
					self.phase_var.set("PACKAGE DONE!")
					self.counter_var.set("")
					pkg_name = item.get("message", "Package")
					self.current_file_var.set("Package generated successfully!")
					self._append_trace(f"[100.0%] ✔ SUCCESS: {pkg_name}", tag="green")
					self._set_buttons_state(tk.NORMAL)
					self._set_status_badge("MOD UP TO DATE", self.C_GREEN_SUCCESS)

					if sys.platform == "win32" and hasattr(self, "hwnd") and self.hwnd:
						try:
							import ctypes
							ctypes.windll.user32.ShowWindow(self.hwnd, 9)
							ctypes.windll.user32.SetForegroundWindow(self.hwnd)
						except Exception:
							pass

					messagebox.showinfo("Success", f"Mod package built successfully!\n\nPackage: {pkg_name}")
				elif typ == "error":
					self._set_buttons_state(tk.NORMAL)
					self._set_status_badge("BUILD FAILED", self.C_RED_ERROR)
					err_msg = item.get("message", "Compilation Error")
					self.current_file_var.set("Compilation Failed")
					self.phase_var.set("ERROR")
					self.counter_var.set("")
					logpath = item.get("logpath")
					if logpath:
						self.error_log_path = Path(logpath)
						self.error_btn.pack(side=tk.LEFT)
					self._append_trace(f"[ERROR] ✖ {err_msg}", tag="red")
					messagebox.showerror("Compilation Error", err_msg)
		except queue.Empty:
			pass
		self.root.after(30, self.poll_queue)

	def start_build(self) -> None:
		pkg_fmt = self.pkg_format_var.get().lower()

		if pkg_fmt == "modpkg":
			options = {
				"accents": True,
				"wards": True,
				"structures": True,
				"minions": True,
				"finishers": True,
				"recalls": True,
				"champions": True,
			}
		else:
			options = {
				"accents": self.chk_accents.get(),
				"wards": self.chk_wards.get(),
				"structures": self.chk_structures.get(),
				"minions": self.chk_minions.get(),
				"finishers": self.chk_finishers.get(),
				"recalls": self.chk_recalls.get(),
				"champions": self.chk_champions.get(),
			}
			if not any(options.values()):
				messagebox.showwarning("Empty Scope", "Please select at least one restoration module to compile!")
				return

		self._set_status_badge("BUILDING...", self.C_YELLOW_ACTIVE)
		self._set_buttons_state(tk.DISABLED)
		self.error_btn.pack_forget()
		self.percent_var.set(0.0)
		self.max_percent = 0.0
		self.last_logged_phase = ""
		self.status_var.set("0.0%")
		self.phase_var.set("INITIALIZING")
		self.counter_var.set("")
		self.current_file_var.set("Preparing workspace...")

		self.trace.configure(state=tk.NORMAL)
		self.trace.delete("1.0", tk.END)
		self.trace.configure(state=tk.DISABLED)

		t = threading.Thread(target=lambda: self.worker_thread(options, pkg_fmt), daemon=True)
		t.start()

	def worker_thread(self, options: dict[str, bool], pkg_fmt: str) -> None:
		try:
			def emit_progress(percent: float, phase: str, status: str, current: str = ""):
				self.queue.put({
					"type": "progress",
					"percent": float(percent),
					"phase": phase,
					"status": status,
					"current_file": current,
				})

			cfg = setup_workspace(self.workspace_root, sync_tools=False, sync_hashes=False)
			champion_wads = cfg.get("champion_wads", [])
			scheduler = ProgressScheduler(options, self.map_wad, self.ui_wad, champion_wads)

			emit_progress(scheduler.pct("tools", 0.0), "PROCESSING TOOLS", "", "Connecting to LTK CI/CD repository...")
			ensure_toolkit_binaries(cfg["tools_dir"], force=False, progress_callback=lambda m: emit_progress(scheduler.pct("tools", 0.5), "Tools", "", m))
			emit_progress(scheduler.pct("tools", 1.0), "TOOLS DONE", "", "LTK tools verified and ready.")

			emit_progress(scheduler.pct("hashes", 0.0), "PROCESSING HASHES", "", "Checking CommunityDragon hashes...")
			ensure_ritobin_hashes(cfg["hashes_dir"], force=False, progress_callback=lambda m: emit_progress(scheduler.pct("hashes", 0.5), "Hashes", "", m))
			emit_progress(scheduler.pct("hashes", 1.0), "HASHES DONE", "", "Hashes verified and synchronized.")

			result = build_vanilla_pipeline(
				self.map_wad,
				self.workspace_root,
				ui_wad=self.ui_wad,
				options=options,
				dry_run=self.dry_run,
				progress_callback=emit_progress,
				cfg=cfg,
				scheduler=scheduler,
				package_format=pkg_fmt,
			)

			if self.dry_run:
				self.queue.put({"type": "done", "message": "Simulation finished; no files generated."})
				return

			pkg_obj = result.get("output_package")
			pkg_name = pkg_obj.name if pkg_obj else f"VanillaTrueMod.{pkg_fmt}"
			self.queue.put({"type": "done", "message": pkg_name})
		except Exception as e:
			import traceback
			detail = traceback.format_exc()
			LOG.error("Compilation failed: %s", e, exc_info=True)
			self.error_log_path.parent.mkdir(parents=True, exist_ok=True)
			try:
				self.error_log_path.write_text(detail, encoding="utf-8")
			except OSError:
				pass
			self.queue.put({"type": "error", "message": f"{e}", "logpath": str(self.error_log_path)})
		finally:
			cleanup_temp_dirs()

	def open_error_log(self) -> None:
		if not self.error_log_path.exists():
			return
		try:
			if os.name == "nt":
				os.startfile(str(self.error_log_path))
			else:
				subprocess.Popen(["xdg-open", str(self.error_log_path)])
		except Exception as e:
			messagebox.showerror("Error", f"Could not open error log: {e}")

	def _check_github_app_update(self) -> None:
		try:
			req = urllib.request.Request(
				GITHUB_LATEST_API,
				headers={"User-Agent": "VanillaBuilder-Client/1.0"}
			)
			with urllib.request.urlopen(req, timeout=5) as resp:
				if resp.status == 200:
					data = json.loads(resp.read().decode("utf-8"))
					latest_tag = data.get("tag_name", "").strip()

					if latest_tag and is_newer_version(latest_tag, APP_VERSION):
						download_url = None
						for asset in data.get("assets", []):
							if asset.get("name") == "VanillaTrueBuilder.zip":
								download_url = asset.get("browser_download_url")
								break

						if not download_url:
							download_url = f"{GITHUB_REPO_URL}/releases/download/{latest_tag}/VanillaTrueBuilder.zip"

						html_url = data.get("html_url") or f"{GITHUB_REPO_URL}/releases/tag/{latest_tag}"
						if getattr(sys, "frozen", False):
							self.root.after(0, lambda: self._start_silent_update(download_url, latest_tag))
						else:
							self.root.after(0, lambda: self._notify_app_update(latest_tag, html_url))
		except Exception:
			pass

	def _start_silent_update(self, download_url: str, latest_tag: str) -> None:
		if hasattr(self, "btn_compile") and str(self.btn_compile["state"]) == "disabled":
			return

		self._set_status_badge(f"UPDATING {latest_tag}...", self.C_YELLOW_ACTIVE)
		self._append_trace(f"[AUTO-UPDATE] New version {latest_tag} detected. Updating silently...", tag="gold")

		def _run_update():
			success = perform_silent_auto_update(
				download_url,
				latest_tag,
				log_callback=lambda msg: self.root.after(0, lambda: self._append_trace(f"[AUTO-UPDATE] {msg}", tag="cyan"))
			)
			if not success:
				self.root.after(0, lambda: self._set_status_badge("UPDATE FAILED", self.C_RED_ERROR))
				self.root.after(0, lambda: self._append_trace("[AUTO-UPDATE] Silent update failed.", tag="red"))

		threading.Thread(target=_run_update, daemon=True).start()

	def _notify_app_update(self, latest_tag: str, html_url: str) -> None:
		self._set_status_badge(f"UPDATE: {latest_tag}", self.C_YELLOW_ACTIVE)
		self._append_trace(f"🚀 New update available: {latest_tag} (Current: {APP_VERSION})", tag="gold")
		
		answer = messagebox.askyesno(
			"Update Available",
			f"A new version of Vanilla True Builder ({latest_tag}) is available!\n\n"
			f"• Current version: {APP_VERSION}\n"
			f"• New version: {latest_tag}\n\n"
			"Would you like to open the download page now?",
			parent=self.root,
		)
		if answer:
			import webbrowser
			webbrowser.open(html_url)

	def on_close(self) -> None:
		cleanup_temp_dirs()
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


def launch_auditor_window():
	try:
		import auditor
		auditor.APP_VERSION = APP_VERSION
		aud_root = tk.Tk()
		aud_app = auditor.ChampionAuditorApp(aud_root)
		aud_root.mainloop()
	except Exception as e:
		messagebox.showerror("Error", f"Failed to launch Auditor: {e}")


if __name__ == "__main__":
	multiprocessing.freeze_support()
	
	if "--auditor" in sys.argv:
		launch_auditor_window()
		sys.exit(0)
		
	try:
		main()
	except KeyboardInterrupt:
		cleanup_temp_dirs()
		raise SystemExit(130)