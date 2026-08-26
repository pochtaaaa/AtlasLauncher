import os
import json
import sys
import subprocess
import urllib.request
import threading
import webview
import zipfile
import platform
import shutil
import tarfile
import traceback
import time
import xml.etree.ElementTree as ET
from pathlib import Path
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

MIRRORS = {
    "mojang": {
        "version_manifest": "https://launchermeta.mojang.com/mc/game/version_manifest.json",
        "libraries_base": "https://libraries.minecraft.net/",
        "assets": "https://resources.download.minecraft.net/",
    },
    "bmclapi": {
        "version_manifest": "https://bmclapi2.bangbang93.com/mc/game/version_manifest.json",
        "libraries_base": "https://bmclapi2.bangbang93.com/maven/",
        "assets": "https://bmclapi2.bangbang93.com/assets/",
    },
    "mcbbs": {
        "version_manifest": "https://download.mcbbs.net/mc/game/version_manifest.json",
        "libraries_base": "https://download.mcbbs.net/maven/",
        "assets": "https://download.mcbbs.net/assets/",
    }
}

ADOPTIUM_API = "https://api.adoptium.net/v3"

THEME_COLORS = {
    "default": {"bg": "", "border": "", "accent": ""},
    "red": {"bg": "rgba(239,68,68,0.08)", "border": "rgba(239,68,68,0.25)", "accent": "#ef4444"},
    "orange": {"bg": "rgba(249,115,22,0.08)", "border": "rgba(249,115,22,0.25)", "accent": "#f97316"},
    "green": {"bg": "rgba(34,197,94,0.08)", "border": "rgba(34,197,94,0.25)", "accent": "#22c55e"},
    "purple": {"bg": "rgba(168,85,247,0.08)", "border": "rgba(168,85,247,0.25)", "accent": "#a855f7"},
    "pink": {"bg": "rgba(236,72,153,0.08)", "border": "rgba(236,72,153,0.25)", "accent": "#ec4899"},
    "cyan": {"bg": "rgba(6,182,212,0.08)", "border": "rgba(6,182,212,0.25)", "accent": "#06b6d4"},
}


class AtlasLauncherAPI:
    def __init__(self):
        self.mc_dir = Path.home() / ".atlas_launcher"
        self.mc_dir.mkdir(exist_ok=True)
        self.versions_dir = self.mc_dir / "versions"
        self.versions_dir.mkdir(exist_ok=True)
        self.libraries_dir = self.mc_dir / "libraries"
        self.libraries_dir.mkdir(exist_ok=True)
        self.assets_dir = self.mc_dir / "assets"
        self.assets_dir.mkdir(exist_ok=True)
        self.java_dir = self.mc_dir / "java"
        self.java_dir.mkdir(exist_ok=True)
        self.current_mirror = "bmclapi"
        self._mirror_auto = True          # пока пользователь не выбрал зеркало вручную
        self._mirror_latency = {}         # name -> ms | None
        self._mirror_scan_done = threading.Event()
        self.current_os = self._detect_os()
        self.current_arch = self._detect_arch()
        self._downloads = {}
        self._java_downloads = {}
        self._active_downloads = set()
        self._lock = threading.Lock()

        # ── Java: кэш найденных путей/версий, чтобы не искать заново на каждый запуск ──
        self._java_cache_file = self.mc_dir / "java_cache.json"
        self._java_version_cache = self._load_java_cache()   # path -> major version | None
        self._java_scan_lock = threading.Lock()
        self._java_scan_done = threading.Event()
        self._system_java_list = []       # [{"path":..., "version":...}], отсортировано по убыванию версии
        self._probe_executor = ThreadPoolExecutor(max_workers=8)

        if HAS_REQUESTS:
            self._session = requests.Session()
            self._session.headers.update({"User-Agent": "AtlasLauncher/1.0"})
        else:
            self._session = None

        # Прогреваем кэши в фоне, не блокируя запуск окна
        threading.Thread(target=self._scan_java, daemon=True).start()
        threading.Thread(target=self.auto_select_mirror, daemon=True).start()
        self.window = None   # выставляется снаружи после webview.create_window(...)

    def _detect_os(self):
        s = platform.system()
        return {"Windows": "windows", "Linux": "linux", "Darwin": "osx"}.get(s, "windows")

    def _detect_arch(self):
        m = platform.machine().lower()
        if m in ("amd64", "x86_64"):
            return "x86_64"
        if m in ("i386", "i686", "x86"):
            return "x86"
        if m in ("arm64", "aarch64"):
            return "arm64"
        return "x86_64"

    def _adoptium_os(self):
        return {"windows": "windows", "linux": "linux", "osx": "mac"}.get(self.current_os, "windows")

    def _adoptium_arch(self):
        return {"x86_64": "x64", "x86": "x32", "arm64": "aarch64"}.get(self.current_arch, "x64")

    def _load_java_cache(self):
        try:
            if self._java_cache_file.exists():
                with open(self._java_cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_java_cache(self):
        try:
            with open(self._java_cache_file, "w", encoding="utf-8") as f:
                json.dump(self._java_version_cache, f)
        except Exception:
            pass

    def _safe_exists(self, path, timeout=0.25):
        """exists() с ограничением по времени — защита от зависших сетевых/облачных путей в PATH"""
        try:
            fut = self._probe_executor.submit(lambda: path.exists())
            return fut.result(timeout=timeout)
        except Exception:
            return False

    def _java_bin_in(self, home_dir):
        exe = "java.exe" if platform.system() == "Windows" else "java"
        p = Path(home_dir) / "bin" / exe
        return p if self._safe_exists(p) else None

    def _registry_java_homes(self):
        """Быстрый поиск установленных JDK/JRE через реестр Windows (без обхода файловой системы)"""
        homes = set()
        if platform.system() != "Windows":
            return homes
        try:
            import winreg
        except ImportError:
            return homes
        roots = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\JavaSoft\JDK"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\JavaSoft\JRE"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\JavaSoft\Java Development Kit"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\JavaSoft\Java Runtime Environment"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\JavaSoft\JDK"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\JavaSoft\JRE"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Eclipse Adoptium\JDK"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Eclipse Adoptium\JRE"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Eclipse Foundation\JDK"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\JDK"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Azul Systems\Zulu"),
        ]
        for hive, path in roots:
            try:
                with winreg.OpenKey(hive, path) as key:
                    i = 0
                    while True:
                        try:
                            subver = winreg.EnumKey(key, i)
                        except OSError:
                            break
                        i += 1
                        try:
                            with winreg.OpenKey(key, subver) as subkey:
                                try:
                                    java_home, _ = winreg.QueryValueEx(subkey, "JavaHome")
                                except OSError:
                                    java_home, _ = winreg.QueryValueEx(subkey, "InstallationPath")
                                if java_home:
                                    homes.add(Path(java_home))
                        except OSError:
                            pass
            except OSError:
                pass
        return homes

    def _common_java_dirs(self):
        """Известные папки, куда официальные установщики Java кладут JRE/JDK"""
        dirs = set()
        system = platform.system()
        if system == "Windows":
            bases = []
            for envvar in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA"):
                v = os.environ.get(envvar)
                if v:
                    bases.append(Path(v))
            subfolders = ["Java", "Eclipse Adoptium", "Eclipse Foundation", "Zulu",
                          "Microsoft\\jdk", "BellSoft", "Programs\\Eclipse Adoptium"]
            for base in bases:
                for sub in subfolders:
                    p = base / sub
                    if self._safe_exists(p):
                        dirs.add(p)
        elif system == "Darwin":
            base = Path("/Library/Java/JavaVirtualMachines")
            if self._safe_exists(base):
                try:
                    for d in base.iterdir():
                        dirs.add(d / "Contents" / "Home")
                except Exception:
                    pass
        else:
            for base_str in ("/usr/lib/jvm", "/opt/java"):
                base = Path(base_str)
                if self._safe_exists(base):
                    try:
                        for d in base.iterdir():
                            dirs.add(d)
                    except Exception:
                        pass
        return dirs

    def _candidate_java_paths(self):
        """Собираем пути к java-бинарю из всех быстрых источников, PATH проверяем последним и с защитой от зависаний"""
        candidates = []
        seen = set()

        def add(p):
            if p is None:
                return
            sp = str(p)
            if sp not in seen:
                seen.add(sp)
                candidates.append(p)

        java_cmd = "java.exe" if platform.system() == "Windows" else "java"

        # 1. JAVA_HOME — мгновенно
        java_home = os.environ.get("JAVA_HOME")
        if java_home:
            add(self._java_bin_in(java_home))

        # 2. Реестр Windows — мгновенно, без обращений к диску
        for home in self._registry_java_homes():
            add(self._java_bin_in(home))

        # 3. Уже скачанные лаунчером версии
        if self.java_dir.exists():
            try:
                for d in self.java_dir.iterdir():
                    if d.is_dir():
                        add(self._java_bin_in(d))
            except Exception:
                pass

        # 4. Известные папки установки (защищённая проверка exists)
        for home in self._common_java_dirs():
            b = self._java_bin_in(home)
            if b:
                add(b)
            elif self._safe_exists(home):
                try:
                    for sub in home.iterdir():
                        add(self._java_bin_in(sub))
                except Exception:
                    pass

        # 5. PATH — последним, с таймаутом на каждую директорию (может содержать сетевые диски)
        for d in os.environ.get("PATH", "").split(os.pathsep):
            if not d:
                continue
            p = Path(d) / java_cmd
            if self._safe_exists(p, timeout=0.15):
                add(p)

        return candidates

    def _probe_java_version(self, java_path):
        """Версия java по пути, с диск-кэшем — повторный запуск лаунчера не будет пере-спрашивать subprocess"""
        key = str(java_path)
        if key in self._java_version_cache:
            return self._java_version_cache[key]
        ver = self._get_java_version(key)
        self._java_version_cache[key] = ver
        return ver

    def _scan_java(self, force=False):
        """Полное сканирование системы на предмет Java, результат кэшируется в памяти и на диске"""
        with self._java_scan_lock:
            if self._system_java_list and not force:
                return self._system_java_list

            candidates = self._candidate_java_paths()
            results = []
            futures = {self._probe_executor.submit(self._probe_java_version, str(p)): p for p in candidates}
            for fut in as_completed(futures, timeout=15):
                try:
                    ver = fut.result(timeout=3)
                except Exception:
                    ver = None
                p = futures[fut]
                if ver:
                    results.append({"path": str(p), "version": ver})

            results.sort(key=lambda x: x["version"], reverse=True)
            self._system_java_list = results
            self._java_scan_done.set()
            self._save_java_cache()
            return results

    def rescan_java(self):
        self._java_version_cache = {}
        self._system_java_list = []
        self._java_scan_done.clear()
        result = self._scan_java(force=True)
        return {"success": True, "found": len(result)}

    def get_java_scan_status(self):
        return {"done": self._java_scan_done.is_set(), "count": len(self._system_java_list)}

    def _find_system_java(self):
        result = self._scan_java()
        return result[0]["path"] if result else None

    def _get_java_version(self, java_path):
        try:
            result = subprocess.run([java_path, "-version"], capture_output=True, text=True, timeout=4)
            output = result.stderr or result.stdout
            import re
            match = re.search(r'version "(\d+)(?:\.(\d+))?', output)
            if match:
                major = int(match.group(1))
                minor = match.group(2)
                if major == 1 and minor:
                    return int(minor)
                return major
        except Exception:
            pass
        return None

    def _fetch_json(self, url, timeout=20):
        if self._session:
            r = self._session.get(url, timeout=timeout)
            r.raise_for_status()
            return r.json()
        else:
            req = urllib.request.Request(url, headers={"User-Agent": "AtlasLauncher/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())

    def _download_file(self, url, dest, timeout=120):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if self._session:
            r = self._session.get(url, timeout=timeout, stream=True)
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=262144):
                    if chunk:
                        f.write(chunk)
        else:
            req = urllib.request.Request(url, headers={"User-Agent": "AtlasLauncher/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                with open(dest, "wb") as f:
                    while True:
                        chunk = resp.read(262144)
                        if not chunk:
                            break
                        f.write(chunk)
        return dest

    def _apply_mirror_to_url(self, url):
        mirror_base = MIRRORS[self.current_mirror]["libraries_base"]
        if "libraries.minecraft.net" in url:
            return url.replace("https://libraries.minecraft.net/", mirror_base)
        return url

    def _check_rules(self, rules):
        if not rules:
            return True
        allowed = False
        for rule in rules:
            action = rule.get("action", "allow")
            os_spec = rule.get("os", {})
            features = rule.get("features", {})
            matches = True
            if "name" in os_spec and os_spec["name"] != self.current_os:
                matches = False
            if "arch" in os_spec and matches:
                if os_spec["arch"] != self.current_arch:
                    matches = False
            if features:
                for feat_name, feat_val in features.items():
                    if feat_val:
                        matches = False
                        break
            if matches:
                allowed = (action == "allow")
        return allowed

    def _resolve_natives_classifier(self, lib):
        natives = lib.get("natives", {})
        key = natives.get(self.current_os)
        if not key:
            return None
        arch_val = "64" if self.current_arch in ("x86_64", "amd64") else "32"
        if self.current_arch == "arm64":
            arch_val = "arm64"
        return key.replace("${arch}", arch_val)

    # ─── PER-VERSION SETTINGS ───
    def _get_version_settings_path(self, version_id):
        return self.versions_dir / version_id / "atlas_settings.json"

    def _load_version_settings(self, version_id):
        path = self._get_version_settings_path(version_id)
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save_version_settings(self, version_id, settings):
        path = self._get_version_settings_path(version_id)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)

    def get_version_settings(self, version_id):
        return {"success": True, "settings": self._load_version_settings(version_id)}

    def set_version_settings(self, version_id, settings):
        try:
            current = self._load_version_settings(version_id)
            current.update(settings)
            self._save_version_settings(version_id, current)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def install_mod_files(self, version_id, file_paths):
        """Копирует перетащенные/выбранные .jar файлы в папку mods версии"""
        try:
            if not file_paths:
                return {"success": False, "error": "Нет файлов"}
            mods_dir = self.versions_dir / version_id / "mods"
            mods_dir.mkdir(parents=True, exist_ok=True)
            installed = []
            errors = []
            for raw_path in file_paths:
                try:
                    src = Path(raw_path)
                    if not src.exists() or not src.is_file():
                        errors.append(f"{src.name}: файл не найден")
                        continue
                    if src.suffix.lower() != ".jar":
                        errors.append(f"{src.name}: не .jar файл")
                        continue
                    dest = mods_dir / src.name
                    shutil.copy2(src, dest)
                    installed.append(src.name)
                except Exception as e:
                    errors.append(f"{Path(raw_path).name}: {e}")
            if not installed and errors:
                return {"success": False, "error": "; ".join(errors)}
            return {"success": True, "installed": installed, "errors": errors}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def pick_and_install_mods(self, version_id):
        """Открывает системный диалог выбора файлов (даёт реальные пути) и ставит выбранные .jar"""
        try:
            if not self.window:
                return {"success": False, "error": "Окно ещё не готово"}
            paths = self.window.create_file_dialog(
                webview.FileDialog.OPEN,
                allow_multiple=True,
                file_types=("Mod files (*.jar)", "All files (*.*)"),
            )
            if not paths:
                return {"success": False, "error": "Отменено"}
            return self.install_mod_files(version_id, list(paths))
        except Exception as e:
            return {"success": False, "error": str(e)}

    def pick_and_install_mrpack(self, version_id):
        """Открывает системный диалог выбора .mrpack и ставит модпак"""
        try:
            if not self.window:
                return {"success": False, "error": "Окно ещё не готово"}
            paths = self.window.create_file_dialog(
                webview.FileDialog.OPEN,
                allow_multiple=False,
                file_types=("Modrinth modpack (*.mrpack)", "All files (*.*)"),
            )
            if not paths:
                return {"success": False, "error": "Отменено"}
            return self.install_mrpack(version_id, paths[0])
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _js_str(self, s):
        return json.dumps(str(s))

    def _notify_ui(self, script):
        try:
            if self.window:
                self.window.evaluate_js(script)
        except Exception:
            pass

    def _on_mod_files_dropped(self, event):
        """Колбэк pywebview.dom для drop на #modDropzone.
        Только через этот путь (window.dom / _dnd_state) pywebview даёт реальный путь
        к перетащенному файлу — обычный File.path в браузерном JS не существует."""
        try:
            files = (event or {}).get("dataTransfer", {}).get("files", []) or []
            jar_paths = [
                f.get("pywebviewFullPath") for f in files
                if f.get("pywebviewFullPath") and str(f.get("pywebviewFullPath")).lower().endswith(".jar")
            ]
            if not jar_paths:
                self._notify_ui(f"window.showToast && showToast({self._js_str('Перетащи файлы с расширением .jar')}, 'error')")
                return

            try:
                version_id = self.window.evaluate_js("typeof drawerVersionId !== 'undefined' ? drawerVersionId : null")
            except Exception:
                version_id = None
            if not version_id:
                self._notify_ui(f"window.showToast && showToast({self._js_str('Сначала открой настройки версии')}, 'error')")
                return

            result = self.install_mod_files(version_id, jar_paths)
            if result.get("success"):
                count = len(result.get("installed", []))
                msg = f"Установлено модов: {count}" if count else "Ни один мод не установлен"
                kind = "success" if count else "error"
                self._notify_ui(
                    f"window.showToast && showToast({self._js_str(msg)}, '{kind}'); "
                    f"window.loadDrawerMods && loadDrawerMods();"
                )
                errs = result.get("errors") or []
                if errs:
                    self._notify_ui(f"window.showToast && showToast({self._js_str('; '.join(errs))}, 'error')")
            else:
                self._notify_ui(f"window.showToast && showToast({self._js_str(result.get('error', 'Ошибка установки'))}, 'error')")
        except Exception:
            self._notify_ui(f"window.showToast && showToast({self._js_str('Ошибка обработки drag-and-drop')}, 'error')")

    def get_version_mods(self, version_id):
        try:
            mods_dir = self.versions_dir / version_id / "mods"
            if not mods_dir.exists():
                return {"success": True, "mods": []}
            mods = []
            for f in mods_dir.iterdir():
                if f.is_file() and f.suffix == ".jar":
                    mods.append({"name": f.name, "size": f.stat().st_size})
            return {"success": True, "mods": mods}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def delete_mod(self, version_id, mod_name):
        try:
            mod_path = self.versions_dir / version_id / "mods" / mod_name
            if mod_path.exists():
                mod_path.unlink()
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def install_mrpack(self, version_id, mrpack_path):
        try:
            mrpack_path = Path(mrpack_path)
            if not mrpack_path.exists():
                return {"success": False, "error": "File not found"}
            version_dir = self.versions_dir / version_id
            mods_dir = version_dir / "mods"
            mods_dir.mkdir(exist_ok=True)
            with zipfile.ZipFile(mrpack_path, "r") as zf:
                if "modrinth.index.json" not in zf.namelist():
                    return {"success": False, "error": "Invalid .mrpack: no modrinth.index.json"}
                with zf.open("modrinth.index.json") as f:
                    index = json.load(f)
                files = index.get("files", [])
                total = len(files)
                for i, file_info in enumerate(files):
                    self._update_progress(version_id, int((i / max(total, 1)) * 100), f"mrpack: {file_info.get('path', '')}")
                    dest_path = version_dir / file_info["path"]
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    downloads = file_info.get("downloads", [])
                    if downloads:
                        try:
                            self._download_file(downloads[0], dest_path)
                        except Exception:
                            pass
                for item in zf.namelist():
                    if item.startswith("overrides/"):
                        dest = version_dir / item.replace("overrides/", "")
                        if item.endswith("/"):
                            dest.mkdir(parents=True, exist_ok=True)
                        else:
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            with zf.open(item) as src, open(dest, "wb") as dst:
                                dst.write(src.read())
            return {"success": True, "message": f"Installed {len(files)} files"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ─── MOD LOADERS ───
    def get_modloaders(self, version_id):
        result = {"fabric": [], "forge": [], "neoforge": [], "quilt": []}
        # Fabric
        try:
            data = self._fetch_json(f"https://meta.fabricmc.net/v2/versions/loader/{version_id}")
            result["fabric"] = [{"version": d["loader"]["version"], "stable": d["loader"]["stable"]} for d in data[:30]]
        except Exception:
            pass
        # Quilt
        try:
            data = self._fetch_json(f"https://meta.quiltmc.org/v3/versions/loader/{version_id}")
            result["quilt"] = [{"version": d["loader"]["version"], "stable": True} for d in data[:30]]
        except Exception:
            pass
        # Forge
        try:
            # Получаем список версий Forge для этой MC версии
            forge_versions = self._get_forge_versions(version_id)
            result["forge"] = forge_versions
        except Exception:
            pass
        # NeoForge
        try:
            neoforge_versions = self._get_neoforge_versions(version_id)
            result["neoforge"] = neoforge_versions
        except Exception:
            pass
        return {"success": True, "loaders": result}

    def _get_forge_versions(self, mc_version):
        """Получает список версий Forge для MC версии"""
        versions = []
        try:
            # Пробуем promotions JSON
            promos = self._fetch_json("https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json")
            promos_data = promos.get("promos", {})
            for key, val in promos_data.items():
                if key.startswith(mc_version + "-"):
                    versions.append({"version": val, "stable": "recommended" in key})
            # Если ничего не нашли — пробуем maven metadata
            if not versions:
                req = urllib.request.Request(
                    "https://maven.minecraftforge.net/net/minecraftforge/forge/maven-metadata.xml",
                    headers={"User-Agent": "AtlasLauncher/1.0"}
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    root = ET.fromstring(resp.read().decode())
                for v in root.findall(".//version"):
                    text = v.text
                    if text and text.startswith(mc_version + "-"):
                        versions.append({"version": text, "stable": False})
                versions = versions[:20]
        except Exception:
            pass
        return versions

    def _get_neoforge_versions(self, mc_version):
        versions = []
        try:
            data = self._fetch_json("https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge")
            for v in data.get("versions", [])[:20]:
                versions.append({"version": v, "stable": False})
        except Exception:
            pass
        return versions

    def get_installed_loader(self, version_id):
        version_dir = self.versions_dir / version_id
        json_path = version_dir / f"{version_id}.json"
        if not json_path.exists():
            return {"success": False, "error": "Version not found"}
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            main_class = data.get("mainClass", "")
            if "fabricmc" in main_class.lower():
                return {"success": True, "loader": "fabric"}
            if "quilt" in main_class.lower():
                return {"success": True, "loader": "quilt"}
            if "neoforge" in main_class.lower():
                return {"success": True, "loader": "neoforge"}
            if "forge" in main_class.lower() or "fml" in main_class.lower():
                return {"success": True, "loader": "forge"}
            return {"success": True, "loader": None}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _get_vanilla_profile(self, version_id):
        """Возвращает ванильный (немодифицированный) version.json, делая бэкап при первом обращении.
        Нужно, потому что install_fabric/quilt/forge/neoforge раньше просто перезаписывали
        {version_id}.json профилем загрузчика — а он у Fabric/Quilt/Forge/NeoForge неполный
        (только "inheritsFrom" + свои библиотеки, без vanilla-либ, ассетов и части аргументов),
        из-за чего игра не запускалась.
        Если версия уже была испорчена ДО этого фикса (json уже содержит inheritsFrom, а бэкапа
        ещё нет) — докачиваем настоящий vanilla-json заново из манифеста, чтобы починить и старые
        установки при переустановке загрузчика."""
        version_dir = self.versions_dir / version_id
        vanilla_path = version_dir / f"{version_id}.vanilla.json"
        json_path = version_dir / f"{version_id}.json"

        if vanilla_path.exists():
            with open(vanilla_path, "r", encoding="utf-8") as f:
                return json.load(f)

        if json_path.exists():
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "inheritsFrom" not in data:
                with open(vanilla_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                return data

        # Либо json ещё нет, либо он уже испорчен старой версией лаунчера — качаем настоящий vanilla
        try:
            manifest = self._fetch_json(MIRRORS[self.current_mirror]["version_manifest"])
            version_info = next((v for v in manifest.get("versions", []) if v["id"] == version_id), None)
            if version_info:
                vanilla_data = self._fetch_json(self._apply_mirror_to_url(version_info["url"]))
                with open(vanilla_path, "w", encoding="utf-8") as f:
                    json.dump(vanilla_data, f, indent=2)
                return vanilla_data
        except Exception:
            pass
        return None

    def _merge_loader_profile(self, vanilla_data, loader_profile):
        """Склеивает vanilla version.json с профилем загрузчика (как это делает inheritsFrom
        в официальном лаунчере), чтобы итоговый json был самодостаточным: с полными библиотеками,
        assetIndex и аргументами, а не только тем, что специфично для загрузчика."""
        if not vanilla_data:
            return loader_profile
        merged = dict(vanilla_data)

        for key in ("mainClass", "id", "type", "assetIndex", "assets", "javaVersion", "complianceLevel"):
            if key in loader_profile:
                merged[key] = loader_profile[key]

        # Библиотеки загрузчика приоритетнее — оставляем первое вхождение группы:артефакта
        merged_libs = list(loader_profile.get("libraries", [])) + list(vanilla_data.get("libraries", []))
        seen = set()
        deduped = []
        for lib in merged_libs:
            name = lib.get("name", "")
            key = ":".join(name.split(":")[:2]) if name else name
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            deduped.append(lib)
        merged["libraries"] = deduped

        v_args = vanilla_data.get("arguments", {})
        c_args = loader_profile.get("arguments", {})
        if v_args or c_args:
            merged["arguments"] = {
                "game": list(v_args.get("game", [])) + list(c_args.get("game", [])),
                "jvm": list(v_args.get("jvm", [])) + list(c_args.get("jvm", [])),
            }
            merged.pop("minecraftArguments", None)
        elif "minecraftArguments" in vanilla_data:
            merged["minecraftArguments"] = vanilla_data["minecraftArguments"]

        merged["inheritsFrom"] = vanilla_data.get("id", "")
        return merged

    def install_fabric(self, version_id, loader_version):
        try:
            version_dir = self.versions_dir / version_id
            if not version_dir.exists():
                return {"success": False, "error": "Version not downloaded"}
            profile_url = f"https://meta.fabricmc.net/v2/versions/loader/{version_id}/{loader_version}/profile/json"
            profile = self._fetch_json(profile_url)
            vanilla = self._get_vanilla_profile(version_id)
            merged = self._merge_loader_profile(vanilla, profile)
            json_path = version_dir / f"{version_id}.json"
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=2)
            libraries = profile.get("libraries", [])
            total = len(libraries)
            for i, lib in enumerate(libraries):
                self._update_progress(version_id, int((i / max(total, 1)) * 100), f"fabric: {lib.get('name', '')}")
                try:
                    artifact = lib.get("downloads", {}).get("artifact")
                    if artifact:
                        lib_path = self.libraries_dir / artifact["path"]
                        lib_path.parent.mkdir(parents=True, exist_ok=True)
                        if not lib_path.exists():
                            self._download_file(artifact["url"], lib_path)
                except Exception:
                    pass
            (version_dir / "mods").mkdir(exist_ok=True)
            return {"success": True, "message": f"Fabric {loader_version} установлен"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def install_quilt(self, version_id, loader_version):
        try:
            version_dir = self.versions_dir / version_id
            if not version_dir.exists():
                return {"success": False, "error": "Version not downloaded"}
            profile_url = f"https://meta.quiltmc.org/v3/versions/loader/{version_id}/{loader_version}/profile/json"
            profile = self._fetch_json(profile_url)
            vanilla = self._get_vanilla_profile(version_id)
            merged = self._merge_loader_profile(vanilla, profile)
            json_path = version_dir / f"{version_id}.json"
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=2)
            libraries = profile.get("libraries", [])
            total = len(libraries)
            for i, lib in enumerate(libraries):
                self._update_progress(version_id, int((i / max(total, 1)) * 100), f"quilt: {lib.get('name', '')}")
                try:
                    artifact = lib.get("downloads", {}).get("artifact")
                    if artifact:
                        lib_path = self.libraries_dir / artifact["path"]
                        lib_path.parent.mkdir(parents=True, exist_ok=True)
                        if not lib_path.exists():
                            self._download_file(artifact["url"], lib_path)
                except Exception:
                    pass
            (version_dir / "mods").mkdir(exist_ok=True)
            return {"success": True, "message": f"Quilt {loader_version} установлен"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def install_forge(self, version_id, forge_version):
        """Устанавливает Forge через installer jar"""
        try:
            version_dir = self.versions_dir / version_id
            if not version_dir.exists():
                return {"success": False, "error": "Version not downloaded"}

            self._update_progress(version_id, 5, "forge: downloading installer")
            installer_url = f"https://maven.minecraftforge.net/net/minecraftforge/forge/{forge_version}/forge-{forge_version}-installer.jar"
            installer_path = version_dir / "forge-installer.jar"
            self._download_file(installer_url, installer_path)

            self._update_progress(version_id, 30, "forge: extracting profile")
            # Распаковываем installer
            with zipfile.ZipFile(installer_path, "r") as zf:
                # Ищем version json
                version_json_name = None
                for name in zf.namelist():
                    if name.endswith(".json") and "version" in name.lower():
                        version_json_name = name
                        break
                if not version_json_name:
                    # Пробуем forge-<version>.json
                    for name in zf.namelist():
                        if name.startswith("forge-") and name.endswith(".json"):
                            version_json_name = name
                            break

                if version_json_name:
                    with zf.open(version_json_name) as f:
                        profile = json.load(f)
                else:
                    # Пробуем install_profile.json
                    with zf.open("install_profile.json") as f:
                        install_profile = json.load(f)
                    profile = install_profile.get("versionInfo", install_profile)

                # Сохраняем version.json (склеиваем с vanilla — профиль форджа сам по себе неполный)
                json_path = version_dir / f"{version_id}.json"
                vanilla = self._get_vanilla_profile(version_id)
                merged = self._merge_loader_profile(vanilla, profile)
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(merged, f, indent=2)

                # Распаковываем все .jar из maven/ в libraries
                for item in zf.namelist():
                    if item.startswith("maven/") and item.endswith(".jar"):
                        dest = self.libraries_dir / item.replace("maven/", "")
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(item) as src, open(dest, "wb") as dst:
                            dst.write(src.read())

            installer_path.unlink()

            # Скачиваем libraries из profile
            libraries = profile.get("libraries", [])
            total = len(libraries)
            for i, lib in enumerate(libraries):
                self._update_progress(version_id, 40 + int((i / max(total, 1)) * 55), f"forge: {lib.get('name', '')}")
                try:
                    downloads = lib.get("downloads", {})
                    artifact = downloads.get("artifact")
                    if artifact:
                        lib_path = self.libraries_dir / artifact["path"]
                        lib_path.parent.mkdir(parents=True, exist_ok=True)
                        if not lib_path.exists():
                            url = self._apply_mirror_to_url(artifact["url"])
                            self._download_file(url, lib_path)
                    # Forge иногда использует url напрямую
                    elif "url" in lib:
                        # Парсим name как group:name:version
                        parts = lib["name"].split(":")
                        if len(parts) >= 3:
                            group = parts[0].replace(".", "/")
                            name = parts[1]
                            version = parts[2]
                            jar_name = f"{name}-{version}.jar"
                            lib_path = self.libraries_dir / group / name / version / jar_name
                            lib_path.parent.mkdir(parents=True, exist_ok=True)
                            if not lib_path.exists():
                                url = lib["url"] + f"{group}/{name}/{version}/{jar_name}"
                                self._download_file(url, lib_path)
                except Exception:
                    pass

            (version_dir / "mods").mkdir(exist_ok=True)
            return {"success": True, "message": f"Forge {forge_version} установлен"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def install_neoforge(self, version_id, neoforge_version):
        """Устанавливает NeoForge через installer jar"""
        try:
            version_dir = self.versions_dir / version_id
            if not version_dir.exists():
                return {"success": False, "error": "Version not downloaded"}

            self._update_progress(version_id, 5, "neoforge: downloading installer")
            installer_url = f"https://maven.neoforged.net/releases/net/neoforged/neoforge/{neoforge_version}/neoforge-{neoforge_version}-installer.jar"
            installer_path = version_dir / "neoforge-installer.jar"
            self._download_file(installer_url, installer_path)

            self._update_progress(version_id, 30, "neoforge: extracting profile")
            with zipfile.ZipFile(installer_path, "r") as zf:
                version_json_name = None
                for name in zf.namelist():
                    if name.endswith(".json") and ("version" in name.lower() or name.startswith("neoforge-")):
                        version_json_name = name
                        break

                if version_json_name:
                    with zf.open(version_json_name) as f:
                        profile = json.load(f)
                else:
                    with zf.open("install_profile.json") as f:
                        install_profile = json.load(f)
                    profile = install_profile.get("versionInfo", install_profile)

                json_path = version_dir / f"{version_id}.json"
                vanilla = self._get_vanilla_profile(version_id)
                merged = self._merge_loader_profile(vanilla, profile)
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(merged, f, indent=2)

                for item in zf.namelist():
                    if item.startswith("maven/") and item.endswith(".jar"):
                        dest = self.libraries_dir / item.replace("maven/", "")
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(item) as src, open(dest, "wb") as dst:
                            dst.write(src.read())

            installer_path.unlink()

            libraries = profile.get("libraries", [])
            total = len(libraries)
            for i, lib in enumerate(libraries):
                self._update_progress(version_id, 40 + int((i / max(total, 1)) * 55), f"neoforge: {lib.get('name', '')}")
                try:
                    downloads = lib.get("downloads", {})
                    artifact = downloads.get("artifact")
                    if artifact:
                        lib_path = self.libraries_dir / artifact["path"]
                        lib_path.parent.mkdir(parents=True, exist_ok=True)
                        if not lib_path.exists():
                            url = self._apply_mirror_to_url(artifact["url"])
                            self._download_file(url, lib_path)
                except Exception:
                    pass

            (version_dir / "mods").mkdir(exist_ok=True)
            return {"success": True, "message": f"NeoForge {neoforge_version} установлен"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def uninstall_loader(self, version_id):
        try:
            version_dir = self.versions_dir / version_id
            json_path = version_dir / f"{version_id}.json"
            jar_path = version_dir / f"{version_id}.jar"
            if not jar_path.exists():
                return {"success": False, "error": "Version not found"}
            manifest = self._fetch_json(MIRRORS[self.current_mirror]["version_manifest"])
            version_info = next((v for v in manifest["versions"] if v["id"] == version_id), None)
            if not version_info:
                return {"success": False, "error": "Version not in manifest"}
            self._download_file(version_info["url"], json_path)
            return {"success": True, "message": "Загрузчик удалён, восстановлена vanilla версия"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ─── API ───
    def _measure_mirror(self, name, urls):
        try:
            req = urllib.request.Request(urls["version_manifest"], headers={"User-Agent": "AtlasLauncher/1.0"}, method="HEAD")
            start = time.time()
            with urllib.request.urlopen(req, timeout=4):
                return name, {"available": True, "latency_ms": round((time.time() - start) * 1000)}
        except Exception:
            return name, {"available": False, "latency_ms": None}

    def get_mirror_status(self):
        results = {}
        with ThreadPoolExecutor(max_workers=len(MIRRORS)) as ex:
            futures = [ex.submit(self._measure_mirror, name, urls) for name, urls in MIRRORS.items()]
            for fut in as_completed(futures, timeout=8):
                try:
                    name, data = fut.result()
                    results[name] = data
                except Exception:
                    pass
        for name in MIRRORS:
            results.setdefault(name, {"available": False, "latency_ms": None})
        with self._lock:
            self._mirror_latency = results
        return results

    def auto_select_mirror(self):
        """Пингует все зеркала и выбирает самое быстрое доступное. Не трогает выбор, если пользователь выбрал зеркало вручную."""
        status = self.get_mirror_status()
        available = {k: v["latency_ms"] for k, v in status.items() if v["available"] and v["latency_ms"] is not None}
        chosen = min(available, key=available.get) if available else "bmclapi"
        self._mirror_scan_done.set()
        if self._mirror_auto:
            self.current_mirror = chosen
        return {"success": True, "chosen": chosen, "status": status, "applied": self._mirror_auto}

    def set_mirror(self, mirror_name):
        if mirror_name == "auto":
            self._mirror_auto = True
            threading.Thread(target=self.auto_select_mirror, daemon=True).start()
            return {"success": True, "mode": "auto"}
        if mirror_name in MIRRORS:
            self._mirror_auto = False
            self.current_mirror = mirror_name
            return {"success": True, "mode": "manual"}
        return {"success": False, "error": "Unknown mirror"}

    def get_current_mirror(self):
        return {"mirror": self.current_mirror, "auto": self._mirror_auto, "auto_scan_done": self._mirror_scan_done.is_set()}

    def get_java_info(self):
        result = self._scan_java()
        if result:
            best = result[0]
            return {"path": best["path"], "version": best["version"], "found": True}
        return {"path": "", "version": None, "found": False}

    def get_managed_java_versions(self):
        versions = []
        if self.java_dir.exists():
            for d in self.java_dir.iterdir():
                if d.is_dir():
                    java_exe = d / "bin" / ("java.exe" if platform.system() == "Windows" else "java")
                    if java_exe.exists():
                        ver = self._probe_java_version(str(java_exe))
                        versions.append({"path": str(java_exe), "version": ver, "dir": str(d.name)})
        return versions

    def get_all_java_options(self):
        options = []
        seen = set()
        for j in self._scan_java():
            if j["path"] in seen:
                continue
            seen.add(j["path"])
            label = f"Java {j['version']} — {Path(j['path']).parent.parent.name}"
            options.append({"path": j["path"], "version": j["version"], "label": label})
        for m in self.get_managed_java_versions():
            if m["path"] in seen:
                continue
            seen.add(m["path"])
            options.append({"path": m["path"], "version": m["version"], "label": f"Atlas Java {m['version']}"})
        options.sort(key=lambda o: (o["version"] or 0), reverse=True)
        return options

    def get_versions(self):
        try:
            data = self._fetch_json(MIRRORS[self.current_mirror]["version_manifest"])
            return {"success": True, "versions": data["versions"], "latest": data.get("latest", {})}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_installed_versions(self):
        installed = []
        if self.versions_dir.exists():
            for item in self.versions_dir.iterdir():
                if item.is_dir() and (item / f"{item.name}.jar").exists():
                    installed.append(item.name)
        return {"success": True, "versions": installed}

    def get_download_progress(self, version_id):
        with self._lock:
            info = self._downloads.get(version_id, {"progress": 0, "status": "idle", "done": False, "error": None})
        return info

    def get_java_download_progress(self):
        with self._lock:
            info = self._java_downloads.copy()
        return info

    def get_active_downloads(self):
        with self._lock:
            return list(self._active_downloads)

    def delete_version(self, version_id):
        try:
            version_dir = self.versions_dir / version_id
            natives_dir = self.mc_dir / "natives" / version_id
            if version_dir.exists():
                shutil.rmtree(version_dir)
            if natives_dir.exists():
                shutil.rmtree(natives_dir)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_theme_colors(self):
        return {"success": True, "themes": THEME_COLORS}

    # ─── JAVA AUTO-DOWNLOAD ───
    def ensure_java(self, version_id):
        json_path = self.versions_dir / version_id / f"{version_id}.json"
        with open(json_path, "r", encoding="utf-8") as f:
            version_data = json.load(f)
        settings = self._load_version_settings(version_id)
        custom_java = settings.get("custom_java_path")
        if custom_java and Path(custom_java).exists():
            ver = self._get_java_version(custom_java)
            return {"success": True, "path": custom_java, "version": ver, "source": "custom"}
        compatible = version_data.get("compatibleJavaMajors", [])
        if not compatible:
            try:
                ver_parts = version_id.split(".")
                major_mc = int(ver_parts[1]) if len(ver_parts) > 1 else 0
                if major_mc >= 21:
                    compatible = [21]
                elif major_mc >= 17:
                    compatible = [21, 17]
                elif major_mc >= 16:
                    compatible = [17, 16]
                else:
                    compatible = [8]
            except Exception:
                compatible = [17]
        needed_major = compatible[0]

        # Сначала смотрим уже просканированные системные Java (кэш, без повторного поиска)
        scanned = self._scan_java()
        best = None
        for j in scanned:
            if j["version"] and j["version"] >= needed_major:
                if best is None or j["version"] < best["version"]:  # ближайшая подходящая, не обязательно самая новая
                    best = j
        if best:
            return {"success": True, "path": best["path"], "version": best["version"], "source": "system"}

        managed = self.get_managed_java_versions()
        for mj in managed:
            if mj["version"] and mj["version"] >= needed_major:
                return {"success": True, "path": mj["path"], "version": mj["version"], "source": "managed"}
        return self._download_java(needed_major)

    def _download_java(self, java_major):
        java_key = f"java_{java_major}"
        with self._lock:
            self._java_downloads[java_key] = {"progress": 0, "status": "starting", "done": False, "error": None}
        try:
            os_name = self._adoptium_os()
            arch = self._adoptium_arch()
            self._update_java_progress(java_key, 5, "fetching_url")
            api_url = f"{ADOPTIUM_API}/binary/latest/{java_major}/ga/{os_name}/{arch}/jre/hotspot/normal/eclipse"
            if self._session:
                r = self._session.head(api_url, allow_redirects=True, timeout=30)
                download_url = r.url
            else:
                req = urllib.request.Request(api_url, headers={"User-Agent": "AtlasLauncher/1.0"}, method="HEAD")
                with urllib.request.urlopen(req, timeout=30) as resp:
                    download_url = resp.geturl()
            self._update_java_progress(java_key, 10, "downloading")
            dest_dir = self.java_dir / f"java-{java_major}"
            if dest_dir.exists():
                shutil.rmtree(dest_dir)
            dest_dir.mkdir(parents=True, exist_ok=True)
            archive_path = dest_dir / f"java{java_major}.archive"
            self._download_file(download_url, archive_path)
            self._update_java_progress(java_key, 70, "extracting")
            if str(archive_path).endswith(".zip"):
                with zipfile.ZipFile(archive_path, "r") as zf:
                    zf.extractall(dest_dir)
            else:
                with tarfile.open(archive_path, "r:*") as tf:
                    tf.extractall(dest_dir)
            archive_path.unlink()
            java_bin = None
            for root, dirs, files in os.walk(dest_dir):
                for f in files:
                    if f == "java.exe" or f == "java":
                        java_bin = Path(root) / f
                        break
                if java_bin:
                    break
            if not java_bin:
                raise Exception("Java binary not found after extraction")
            self._update_java_progress(java_key, 100, "completed", done=True)
            ver = self._get_java_version(str(java_bin))
            return {"success": True, "path": str(java_bin), "version": ver, "source": "downloaded"}
        except Exception as e:
            self._update_java_progress(java_key, 0, f"error: {str(e)}", done=True, error=str(e))
            return {"success": False, "error": str(e)}

    def _update_java_progress(self, key, progress, status, done=False, error=None):
        with self._lock:
            self._java_downloads[key] = {"progress": progress, "status": status, "done": done, "error": error}

    # ─── СКАЧИВАНИЕ ВЕРСИИ ───
    def download_version(self, version_id):
        def do_download():
            self._update_progress(version_id, 1, "starting")
            try:
                manifest = self._fetch_json(MIRRORS[self.current_mirror]["version_manifest"])
                version_info = next((v for v in manifest["versions"] if v["id"] == version_id), None)
                if not version_info:
                    self._finish_download(version_id, error="Version not found in manifest")
                    return
                version_dir = self.versions_dir / version_id
                version_dir.mkdir(exist_ok=True)
                json_path = version_dir / f"{version_id}.json"
                jar_path = version_dir / f"{version_id}.jar"
                self._update_progress(version_id, 3, "downloading_json")
                self._download_file(version_info["url"], json_path)
                with open(json_path, "r", encoding="utf-8") as f:
                    version_data = json.load(f)
                self._update_progress(version_id, 8, "downloading_client")
                self._download_file(version_data["downloads"]["client"]["url"], jar_path)
                self._update_progress(version_id, 12, "downloading_libraries")
                self._download_libraries_parallel(version_data, version_id)
                self._update_progress(version_id, 55, "downloading_assets_index")
                asset_index = version_data.get("assetIndex", {})
                if asset_index:
                    index_dir = self.assets_dir / "indexes"
                    index_dir.mkdir(exist_ok=True)
                    index_path = index_dir / f"{asset_index['id']}.json"
                    if not index_path.exists():
                        self._download_file(asset_index["url"], index_path)
                    self._update_progress(version_id, 60, "downloading_assets")
                    self._download_assets_parallel(index_path, version_id)
                self._update_progress(version_id, 92, "extracting_natives")
                self._extract_natives(version_id, version_data)
                self._finish_download(version_id)
            except Exception as e:
                self._finish_download(version_id, error=str(e))
            finally:
                with self._lock:
                    self._active_downloads.discard(version_id)
        with self._lock:
            if version_id in self._active_downloads:
                return {"success": False, "error": "Already downloading"}
            self._active_downloads.add(version_id)
            self._downloads[version_id] = {"progress": 0, "status": "starting", "done": False, "error": None}
        threading.Thread(target=do_download, daemon=True).start()
        return {"success": True}

    def _update_progress(self, version_id, progress, status):
        with self._lock:
            if version_id in self._downloads:
                self._downloads[version_id]["progress"] = progress
                self._downloads[version_id]["status"] = status

    def _finish_download(self, version_id, error=None):
        with self._lock:
            self._downloads[version_id]["done"] = True
            self._downloads[version_id]["error"] = error
            if error:
                self._downloads[version_id]["status"] = f"error: {error}"
                self._downloads[version_id]["progress"] = 0
            else:
                self._downloads[version_id]["status"] = "completed"
                self._downloads[version_id]["progress"] = 100

    def _download_libraries_parallel(self, version_data, version_id):
        libraries = version_data.get("libraries", [])
        to_download = []
        for lib in libraries:
            if not self._check_rules(lib.get("rules", [])):
                continue
            downloads = lib.get("downloads", {})
            artifact = downloads.get("artifact")
            if artifact:
                p = self.libraries_dir / artifact["path"]
                if not p.exists():
                    to_download.append((self._apply_mirror_to_url(artifact["url"]), p))
            classifier = self._resolve_natives_classifier(lib)
            if classifier:
                ci = downloads.get("classifiers", {}).get(classifier)
                if ci:
                    p = self.libraries_dir / ci["path"]
                    if not p.exists():
                        to_download.append((self._apply_mirror_to_url(ci["url"]), p))
        total = len(to_download)
        if total == 0:
            return
        completed = [0]
        lock = threading.Lock()
        def dl(item):
            try:
                self._download_file(item[0], item[1])
            except Exception:
                pass
            with lock:
                completed[0] += 1
                self._update_progress(version_id, 12 + int((completed[0] / total) * 43), "downloading_libraries")
        with ThreadPoolExecutor(max_workers=10) as ex:
            ex.map(dl, to_download)

    def _download_assets_parallel(self, index_path, version_id):
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        objects = list(data.get("objects", {}).items())
        mirror_assets = MIRRORS[self.current_mirror]["assets"]
        to_download = []
        for name, info in objects:
            h = info["hash"]
            prefix = h[:2]
            p = self.assets_dir / "objects" / prefix / h
            if not p.exists():
                to_download.append((f"{mirror_assets}{prefix}/{h}", p))
        total = len(to_download)
        if total == 0:
            return
        completed = [0]
        lock = threading.Lock()
        def dl(item):
            try:
                self._download_file(item[0], item[1])
            except Exception:
                pass
            with lock:
                completed[0] += 1
                self._update_progress(version_id, 60 + int((completed[0] / total) * 32), "downloading_assets")
        with ThreadPoolExecutor(max_workers=12) as ex:
            ex.map(dl, to_download)

    def _extract_natives(self, version_id, version_data):
        natives_dir = self.mc_dir / "natives" / version_id
        if natives_dir.exists():
            shutil.rmtree(natives_dir)
        natives_dir.mkdir(parents=True, exist_ok=True)
        for lib in version_data.get("libraries", []):
            try:
                if not self._check_rules(lib.get("rules", [])):
                    continue
                classifier = self._resolve_natives_classifier(lib)
                if not classifier:
                    continue
                ci = lib.get("downloads", {}).get("classifiers", {}).get(classifier)
                if not ci:
                    continue
                p = self.libraries_dir / ci["path"]
                if not p.exists():
                    continue
                excludes = set()
                if "extract" in lib and "exclude" in lib["extract"]:
                    excludes = set(lib["extract"]["exclude"])
                with zipfile.ZipFile(p, "r") as zf:
                    for item in zf.namelist():
                        skip = any(item.startswith(ex) for ex in excludes)
                        if skip:
                            continue
                        dest_name = item
                        if self.current_os == "osx" and dest_name.endswith(".jnilib"):
                            dest_name = dest_name[:-7] + ".dylib"
                        dest = natives_dir / dest_name
                        if item.endswith("/"):
                            dest.mkdir(parents=True, exist_ok=True)
                        else:
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            with zf.open(item) as src, open(dest, "wb") as dst:
                                dst.write(src.read())
            except Exception:
                pass

    # ─── ЗАПУСК ───
    def launch_game(self, version_id, username, ram_mb=None):
        try:
            version_dir = self.versions_dir / version_id
            json_path = version_dir / f"{version_id}.json"
            jar_path = version_dir / f"{version_id}.jar"
            if not jar_path.exists():
                return {"success": False, "error": "Version not downloaded"}
            with open(json_path, "r", encoding="utf-8") as f:
                version_data = json.load(f)
            settings = self._load_version_settings(version_id)
            if ram_mb is None:
                ram_mb = settings.get("ram", 2048)
            java_result = self.ensure_java(version_id)
            if not java_result["success"]:
                return {"success": False, "error": f"Java error: {java_result.get('error', 'unknown')}"}
            java_path = java_result["path"]
            natives_dir = self.mc_dir / "natives" / version_id
            asset_index_id = version_data.get("assetIndex", {}).get("id", version_id)
            offline_uuid = str(uuid.uuid3(uuid.NAMESPACE_OID, f"OfflinePlayer:{username}"))
            classpath_jars = []
            for lib in version_data.get("libraries", []):
                if not self._check_rules(lib.get("rules", [])):
                    continue
                downloads = lib.get("downloads", {})
                artifact = downloads.get("artifact")
                if artifact:
                    fpath = self.libraries_dir / artifact["path"]
                    if fpath.exists():
                        classpath_jars.append(str(fpath))
                classifier = self._resolve_natives_classifier(lib)
                if classifier:
                    ci = downloads.get("classifiers", {}).get(classifier)
                    if ci:
                        fpath = self.libraries_dir / ci["path"]
                        if fpath.exists():
                            classpath_jars.append(str(fpath))
            classpath_jars.append(str(jar_path))
            classpath = os.pathsep.join(classpath_jars)
            placeholders = {
                "${natives_directory}": str(natives_dir),
                "${library_directory}": str(self.libraries_dir),
                "${classpath}": classpath,
                "${launcher_name}": "AtlasLauncher",
                "${launcher_version}": "1.0",
                "${version_name}": version_id,
                "${game_directory}": str(self.mc_dir),
                "${assets_root}": str(self.assets_dir),
                "${assets_index_name}": asset_index_id,
                "${auth_uuid}": offline_uuid,
                "${auth_access_token}": "0",
                "${auth_player_name}": username,
                "${user_type}": "legacy",
                "${version_type}": version_data.get("type", "release"),
            }
            jvm_args = [f"-Xmx{ram_mb}M"]
            if "arguments" in version_data and "jvm" in version_data["arguments"]:
                jvm_args.extend(self._process_args_list(version_data["arguments"]["jvm"], placeholders))
            else:
                jvm_args.append(f"-Djava.library.path={natives_dir}")
                jvm_args.append("-cp")
                jvm_args.append(classpath)
            mc_args = []
            if "arguments" in version_data and "game" in version_data["arguments"]:
                mc_args.extend(self._process_args_list(version_data["arguments"]["game"], placeholders))
            elif "minecraftArguments" in version_data:
                raw = version_data["minecraftArguments"]
                for k, v in placeholders.items():
                    raw = raw.replace(k, v)
                mc_args.extend(raw.split())
            else:
                mc_args.extend([
                    "--username", username, "--version", version_id,
                    "--gameDir", str(self.mc_dir), "--assetsDir", str(self.assets_dir),
                    "--assetIndex", asset_index_id, "--uuid", offline_uuid,
                    "--accessToken", "0", "--userType", "legacy",
                    "--versionType", version_data.get("type", "release"),
                ])
            main_class = version_data.get("mainClass", "net.minecraft.client.main.Main")
            cmd = [java_path] + jvm_args + [main_class] + mc_args

            logs_dir = self.mc_dir / "launcher_logs"
            logs_dir.mkdir(exist_ok=True)
            log_path = logs_dir / f"{version_id}_latest.log"
            log_file = open(log_path, "w", encoding="utf-8", errors="replace")
            log_file.write("=== CMD ===\n" + " ".join(cmd) + "\n=== OUTPUT ===\n")
            log_file.flush()
            proc = subprocess.Popen(cmd, cwd=self.mc_dir, stdout=log_file, stderr=subprocess.STDOUT)
            log_file.close()
            return {
                "success": True,
                "java_version": java_result.get("version"),
                "java_source": java_result.get("source"),
                "pid": proc.pid,
                "log_path": str(log_path),
            }
        except Exception as e:
            return {"success": False, "error": str(e) + "\n" + traceback.format_exc()}

    def get_launch_log(self, version_id, tail_lines=80):
        """Последние строки лога последнего запуска — полезно, если игра крашится сразу после старта"""
        try:
            log_path = self.mc_dir / "launcher_logs" / f"{version_id}_latest.log"
            if not log_path.exists():
                return {"success": False, "error": "Лог не найден"}
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            return {"success": True, "log": "".join(lines[-tail_lines:]), "path": str(log_path)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _process_args_list(self, args_list, placeholders):
        result = []
        for item in args_list:
            if isinstance(item, str):
                val = item
                for k, v in placeholders.items():
                    val = val.replace(k, v)
                result.append(val)
            elif isinstance(item, dict):
                if "rules" in item and not self._check_rules(item["rules"]):
                    continue
                value = item.get("value", "")
                if isinstance(value, list):
                    for v in value:
                        val = v
                        for k, vph in placeholders.items():
                            val = val.replace(k, vph)
                        result.append(val)
                elif isinstance(value, str):
                    val = value
                    for k, vph in placeholders.items():
                        val = val.replace(k, vph)
                    result.append(val)
        return result


if __name__ == "__main__":
    api = AtlasLauncherAPI()
    window = webview.create_window(
        "Atlas Launcher", "frontend/index.html",
        js_api=api, width=1280, height=800,
        resizable=True, min_size=(950, 650)
    )
    api.window = window

    # Реальный путь к перетащенному файлу pywebview даёт только через свой DOM API
    # (window.dom / events.drop), обычный браузерный drag&drop его не содержит.
    try:
        from webview.dom import DOMEventHandler

        def _register_mod_dropzone():
            try:
                el = window.dom.get_element('#modDropzone')
                if el:
                    el.events.drop += DOMEventHandler(
                        api._on_mod_files_dropped, prevent_default=True, stop_propagation=True
                    )
            except Exception as e:
                print("Не удалось подключить drag-and-drop модов:", e)

        window.events.loaded += _register_mod_dropzone
    except ImportError:
        pass

    webview.start(debug=False)
