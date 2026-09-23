# PyInstaller ビルド設定
# Windows のプロジェクトルートで実行:
#   pip install -r requirements.txt pyinstaller
#   pyinstaller scripts/misty.spec
# dist/MistyPOS/MistyPOS.exe が生成される。
import os

block_cipher = None
project_root = os.path.abspath(os.path.join(os.path.dirname(SPECPATH), "."))

a = Analysis(
    [os.path.join(project_root, "run.py")],
    pathex=[project_root],
    binaries=[],
    datas=[
        (os.path.join(project_root, "misty", "templates"), "misty/templates"),
        (os.path.join(project_root, "misty", "static"), "misty/static"),
    ],
    hiddenimports=["qrcode", "PIL", "PIL._imaging", "waitress"],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MistyPOS",
    debug=False,
    strip=False,
    upx=False,
    console=True,  # 当日のトラブル対応でログを目視できるよう、コンソール窓を残す
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="MistyPOS",
)
