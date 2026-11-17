"""Generate a local, Git-ignored activation configuration before packaging."""
from pathlib import Path
import argparse
import getpass
import hashlib
import os

ROOT = Path(__file__).resolve().parents[1]


def write_config(code: str, target: Path):
    code = code.strip()
    if not code:
        raise ValueError("激活码不能为空。")
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
    target.write_text('# Local build configuration. Do not commit.\n'
                      f'ACTIVATION_CODE_SHA256 = "{digest}"\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--from-env', action='store_true', help='Read ORDER_EXPORTER_ACTIVATION_CODE')
    args = parser.parse_args()
    code = os.environ.get('ORDER_EXPORTER_ACTIVATION_CODE', '') if args.from_env else getpass.getpass('设置本次构建的激活码：')
    write_config(code, ROOT / 'order_exporter' / '_private_config.py')
    print('本机激活配置已保存，不会加入 Git 提交。')


if __name__ == '__main__':
    main()
