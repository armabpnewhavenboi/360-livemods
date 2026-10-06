# PyInstaller build recipe:  pyinstaller --noconfirm 360LiveMods.spec
from PyInstaller.utils.hooks import collect_data_files

datas = collect_data_files("customtkinter")
datas += [("livemods/games/*.json", "livemods/games"), ("livemods/games/*.jpg", "livemods/games"),
          ("assets", "assets")]

a = Analysis(["run.py"], pathex=["."], datas=datas, hiddenimports=["PIL._tkinter_finder"],
             excludes=["pytest", "numpy"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="360LiveMods", console=False,
          icon="assets/icon.ico")
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="360LiveMods")
