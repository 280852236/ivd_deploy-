import os

_templates = {}


def load_templates(template_dir):
    global _templates
    template_files = {
        'MAIN_HTML': 'main.html',
        'BUGS_HTML': 'bugs.html',
        'LIS_ISSUES_HTML': 'lis_issues.html',
        'HARDWARE_FAILURES_HTML': 'hardware_failures.html',
        'BOARD_COMPATIBILITY_HTML': 'board_compatibility.html',
        'ADMIN_HTML': 'admin.html',
        'ANALYSIS_HTML': 'analysis.html',
        'AI_CHAT_HTML': 'ai_chat.html',
        'LOGIN_HTML': 'login.html',
        'REGISTER_HTML': 'register.html',
    }
    for name, filename in template_files.items():
        filepath = os.path.join(template_dir, filename)
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                _templates[name] = f.read()


def get_template(name):
    return _templates.get(name, '')


def set_template(name, content):
    _templates[name] = content