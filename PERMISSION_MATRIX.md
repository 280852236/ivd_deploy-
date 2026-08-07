# IVD平台 权限矩阵
> 自动生成于 2026-08-04 06:30

## 公开路由（无需认证）

| 路径 | 方法 | 权限 | 端点 |
|------|------|------|------|
| /admin/login | GET, POST | 公开 | admin.admin_login |
| /api/analysis/<analysis_id> | GET | 公开 | analysis.get_analysis_data |
| /api/analysis/<analysis_id>/file | GET | 公开 | analysis.get_analysis_file |
| /api/analysis/<analysis_id>/tree | GET | 公开 | analysis.get_analysis_tree |
| /api/board-compat/bootloader | GET | 公开 | hardware.get_bootloader_compat |
| /api/board-compat/pcba | GET | 公开 | hardware.get_pcba_compat |
| /api/bugs | GET | 公开 | bugs.get_bugs |
| /api/bugs/<model>/<int:bug_id>/images | GET | 公开 | bugs.get_bug_images |
| /api/bugs/<model>/<int:bug_id>/images/<int:image_id> | GET | 公开 | bugs.get_bug_image |
| /api/bugs/search | GET | 公开 | bugs.search_bugs |
| /api/bugs/versions | GET | 公开 | bugs.get_bug_versions |
| /api/hardware-failures | GET | 公开 | hardware.get_hardware_failures |
| /api/hardware-failures/<model>/<int:failure_id>/images | GET | 公开 | hardware.get_hardware_failure_images |
| /api/hardware-failures/<model>/<int:failure_id>/images/<int:image_id> | GET | 公开 | hardware.get_hardware_failure_image |
| /api/hardware-failures/search | GET | 公开 | hardware.search_hardware_failures |
| /api/lis/templates | GET | 公开 | lis.lis_list_templates |
| /api/lis/templates/<int:tid>/pdf | GET | 公开 | lis.lis_download_template_pdf |
| /api/lis/templates/<series>/<model> | GET | 公开 | lis.lis_get_template |
| /api/models | GET | 公开 | admin.get_models |
| /api/motor_status | GET | 公开 | admin.get_motor_status |
| /api/rules | GET | 公开 | admin.get_rules_api |
| /api/series | GET | 公开 | admin.get_series |
| /api/task_status/<analysis_id> | GET | 公开 | analysis.task_status |
| /api/verify_super_admin | POST | 公开 | admin.verify_super_admin |
| /register | GET, POST | 公开 | admin.register |

## 登录用户路由

| 路径 | 方法 | 权限 | 端点 |
|------|------|------|------|
| / | GET | 登录 | admin.index |
| /admin/logout | GET | 登录 | admin.admin_logout |
| /admin/rules | GET | 登录 | admin.admin_rules |
| /analysis/<analysis_id> | GET | 登录 | analysis.analysis_view |
| /api/analysis/<analysis_id>/load-more | POST | 登录 | analysis.load_more_files |
| /api/analyze | POST | 登录 | analysis.analyze_file |
| /api/board-compat/bootloader | POST | 登录 | hardware.add_bootloader_compat |
| /api/board-compat/bootloader/<model>/<int:row_id> | PUT | 登录 | hardware.update_bootloader_compat |
| /api/board-compat/bootloader/<model>/<int:row_id> | DELETE | 登录 | hardware.delete_bootloader_compat |
| /api/board-compat/bootloader/import | POST | 登录 | hardware.import_bootloader_compat |
| /api/board-compat/pcba | POST | 登录 | hardware.add_pcba_compat |
| /api/board-compat/pcba/<model>/<int:row_id> | PUT | 登录 | hardware.update_pcba_compat |
| /api/board-compat/pcba/<model>/<int:row_id> | DELETE | 登录 | hardware.delete_pcba_compat |
| /api/board-compat/pcba/import | POST | 登录 | hardware.import_pcba_compat |
| /api/bugs | POST | 登录 | bugs.add_bug |
| /api/bugs/<model>/<int:bug_id> | PUT | 登录 | bugs.update_bug |
| /api/bugs/<model>/<int:bug_id> | DELETE | 登录 | bugs.delete_bug |
| /api/bugs/<model>/<int:bug_id>/image | DELETE | 登录 | bugs.delete_all_bug_images |
| /api/bugs/<model>/<int:bug_id>/images/<int:image_id> | DELETE | 登录 | bugs.delete_bug_image |
| /api/hardware-failures | POST | 登录 | hardware.add_hardware_failure |
| /api/hardware-failures/<model>/<int:failure_id> | PUT | 登录 | hardware.update_hardware_failure |
| /api/hardware-failures/<model>/<int:failure_id> | DELETE | 登录 | hardware.delete_hardware_failure |
| /api/hardware-failures/<model>/<int:failure_id>/images/<int:image_id> | DELETE | 登录 | hardware.delete_hardware_failure_image |
| /api/lis/parse-log | POST | 登录 | lis.lis_parse_log |
| /api/lis/templates | POST | 登录 | lis.lis_upload_template |
| /api/lis/templates/<int:tid> | DELETE | 登录 | lis.lis_delete_template |
| /api/lis/templates/<int:tid>/content | PUT | 登录 | lis.lis_update_template_content |
| /api/search-in-files | POST | 登录 | analysis.search_in_files |
| /board-compatibility | GET | 登录 | hardware.board_compatibility_page |
| /bugs | GET | 登录 | bugs.bugs_page |
| /hardware-failures | GET | 登录 | hardware.hardware_failures_page |
| /lis-issues | GET | 登录 | lis.lis_issues_page |

## 超管路由

| 路径 | 方法 | 权限 | 端点 |
|------|------|------|------|
| /api/import_pdf | POST | 超管 | analysis.import_pdf |
| /api/motor_status/clear | DELETE | 超管 | admin.clear_motor_status |
| /api/rules | POST | 超管 | admin.add_rule_api |
| /api/rules/<int:rule_id> | PUT | 超管 | admin.update_rule_api |
| /api/rules/<int:rule_id> | DELETE | 超管 | admin.delete_rule_api |
| /api/users | GET | 超管 | admin.list_users |
| /api/users/<int:user_id> | DELETE | 超管 | admin.delete_user |
| /api/users/<int:user_id>/permission | PUT | 超管 | admin.update_user_permission |
| /api/users/<int:user_id>/reset-password | PUT | 超管 | admin.reset_user_password |
| /api/users/<int:user_id>/toggle-active | PUT | 超管 | admin.toggle_user_active |

**总计**: 公开 25 个, 登录 32 个, 超管 10 个
