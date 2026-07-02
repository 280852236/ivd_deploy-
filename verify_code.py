#!/usr/bin/env python3
"""
IVD故障分析平台 - 代码验证脚本
检查所有Python文件的语法和导入
"""

import sys
import os
from py_compile import compile

def check_syntax(filepath):
    """检查Python文件语法"""
    try:
        compile(filepath, doraise=True)
        return True, "语法正确"
    except SyntaxError as e:
        return False, f"语法错误: {e}"

def check_import(filepath, module_name):
    """检查模块导入"""
    try:
        sys.path.insert(0, os.path.dirname(filepath))
        module = __import__(module_name)
        return True, "导入成功"
    except Exception as e:
        return False, f"导入失败: {e}"

def main():
    print("=" * 60)
    print("  IVD故障分析平台 - 代码验证")
    print("=" * 60)
    print()
    
    files_to_check = [
        ("/home/ivduser/ivd_analyzer/app.py", "app"),
        ("/home/ivduser/ivd_analyzer/tasks.py", "tasks"),
        ("/home/ivduser/ivd_analyzer/celery_app.py", "celery_app"),
        ("/home/ivduser/ivd_deploy/web/app.py", "app"),
        ("/home/ivduser/ivd_deploy/web/tasks.py", "tasks"),
        ("/home/ivduser/ivd_deploy/web/celery_app.py", "celery_app"),
    ]
    
    all_passed = True
    
    for filepath, module_name in files_to_check:
        if not os.path.exists(filepath):
            print(f"⚠️  文件不存在: {filepath}")
            continue
            
        print(f"检查: {filepath}")
        
        # 语法检查
        syntax_ok, syntax_msg = check_syntax(filepath)
        if syntax_ok:
            print(f"  ✅ {syntax_msg}")
        else:
            print(f"  ❌ {syntax_msg}")
            all_passed = False
            continue
        
        # 导入检查（只检查主要文件）
        if module_name == "app":
            import_ok, import_msg = check_import(filepath, module_name)
            if import_ok:
                print(f"  ✅ {import_msg}")
            else:
                print(f"  ❌ {import_msg}")
                all_passed = False
        
        print()
    
    print("=" * 60)
    if all_passed:
        print("  ✅ 所有检查通过！")
        print("=" * 60)
        return 0
    else:
        print("  ❌ 存在错误，请修复")
        print("=" * 60)
        return 1

if __name__ == "__main__":
    sys.exit(main())