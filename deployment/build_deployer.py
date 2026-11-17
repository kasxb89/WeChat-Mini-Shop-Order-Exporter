"""构建离线部署器；测试仅写入 verification/deploy-sandbox，不修改真实安装目录。"""
from pathlib import Path
from datetime import datetime
import hashlib
import json
import os
import subprocess
import sys

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
from order_exporter import __version__

version=__version__.split("-")[0]
release=root/"dist"/version
application=release/"微信小店订单导出器.exe"
compiler=Path(os.environ["WINDIR"])/"Microsoft.NET/Framework64/v4.0.30319/csc.exe"
if not application.is_file() or not compiler.is_file():
    raise SystemExit("请先构建主程序，并使用带 .NET Framework 编译器的 Windows 构建机。")
digest=hashlib.sha256(application.read_bytes()).hexdigest()
build=root/"build"/version/"deployment"
build.mkdir(parents=True,exist_ok=True)
source=build/"Deploy.cs"
source.write_text((root/"deployment/Deploy.cs").read_text(encoding="utf-8").replace("__VERSION__",version).replace("__PAYLOAD_SHA256__",digest),encoding="utf-8-sig")
common=[str(compiler),"/nologo","/target:winexe","/platform:anycpu","/optimize+","/utf8output",
        "/r:System.Windows.Forms.dll","/r:System.Drawing.dll","/r:Microsoft.CSharp.dll",
        f"/win32manifest:{root/'deployment/app.manifest'}",f"/win32icon:{root/'order_exporter/assets/app.ico'}",
        f"/resource:{application},exporter-payload",f"/resource:{root/'使用说明.txt'},exporter-usage"]
setup=release/"微信小店订单导出器-一键部署.exe"
qa=build/"DeploymentTests.exe"
for output,flags in ((setup,[]),(qa,["/define:QA"])):
    subprocess.run(common+flags+[f"/out:{output}",str(source)],check=True)
subprocess.run([str(setup),"--verify"],check=True,timeout=30)
sandbox=root/"verification/deploy-sandbox"/datetime.now().strftime("%Y%m%d_%H%M%S")
sandbox=sandbox.resolve()
if not sandbox.is_relative_to((root/"verification/deploy-sandbox").resolve()):
    raise SystemExit("测试路径越界")
subprocess.run([str(qa),"--self-test",str(sandbox)],check=True,timeout=60)
assert hashlib.sha256((sandbox/application.name).read_bytes()).hexdigest()==digest
assert (sandbox/"保留数据.txt").read_text(encoding="utf-8-sig")=="preserved"
result={"version":__version__,"setup":str(setup),"bytes":setup.stat().st_size,
        "sha256":hashlib.sha256(setup.read_bytes()).hexdigest(),"payload_sha256":digest,
        "payload_verification":True,"first_deployment_and_upgrade":True,"existing_files_preserved":True,
        "shortcut_creation_and_target_verified_in_sandbox":True,
        "selftest_directory":str(sandbox),"shortcuts_on_clean_customer_machine_verified":False,
        "system_runtime_installation_required":False}
(root/f"verification/deployment-check-{version}.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps(result,ensure_ascii=False))
