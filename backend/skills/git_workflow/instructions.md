# Git Workflow 标准作业程序 (SOP)

## 1. 日常提交流程

```bash
# 查看当前状态
git status

# 暂存所有修改
git add .

# 或选择性暂存
git add <file1> <file2>

# 提交（遵循 Conventional Commits 规范）
git commit -m "feat: 添加用户登录功能"
git commit -m "fix: 修复分页显示错误"
git commit -m "docs: 更新 API 文档"
git commit -m "refactor: 重构配置加载逻辑"
```

### Conventional Commits 类型
| 类型 | 说明 |
|------|------|
| `feat` | 新功能 |
| `fix` | Bug 修复 |
| `docs` | 文档变更 |
| `style` | 代码格式（不影响功能） |
| `refactor` | 代码重构（不新增功能、不修复 Bug） |
| `perf` | 性能优化 |
| `test` | 测试相关 |
| `chore` | 构建/工具变更 |

## 2. 分支管理

```bash
# 创建并切换到新分支
git checkout -b feature/my-feature

# 查看所有分支
git branch -a

# 切换分支
git checkout main

# 删除已合并的分支
git branch -d feature/old-feature

# 强制删除未合并的分支
git branch -D feature/abandoned-feature
```

## 3. 合并冲突解决

```bash
# 1. 拉取最新代码
git fetch origin
git merge origin/main

# 2. 如果出现冲突，查看冲突文件
git status

# 3. 手动编辑冲突文件，移除冲突标记
#    <<<<<<< HEAD
#    你的修改
#    =======
#    远程的修改
#    >>>>>>> origin/main

# 4. 标记冲突已解决
git add <resolved-file>

# 5. 完成合并
git commit -m "merge: 合并 main 分支，解决冲突"
```

## 4. 撤销操作

```bash
# 撤销工作区修改（未暂存）
git checkout -- <file>

# 取消暂存
git reset HEAD <file>

# 撤销最近一次提交（保留修改）
git reset --soft HEAD~1

# 查看提交历史
git log --oneline -20
```

## 5. 远程操作

```bash
# 推送到远程
git push origin <branch-name>

# 首次推送新分支
git push -u origin <branch-name>

# 拉取远程更新
git pull origin main
```
