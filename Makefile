# MusicTidy — 顶层 Makefile
# 主要给 demo 部署用。dev 在本地工作站，demo 在你的 VPS / NAS（musictidy 用户）。
#
# 流程：
#   1) 改代码 → commit + push 到 GitHub
#   2) `make deploy` → ssh 上 VPS git pull + systemctl restart + 等待 healthz 200
#
# Stage 系统：先 push 再 deploy，两步分开。push 自动了你忘了 deploy 也只是
# demo 没更新而不是 demo 跑错版本。

SHELL := /bin/bash

# ─── 配置 ────────────────────────────────────────────────────
DEMO_HOST       := musictidy-demo
DEMO_REPO_DIR   := /home/musictidy/repo
DEMO_HEALTH_URL := https://demo.musictidy.com/healthz
DEMO_BRANCH     := main

# ─── help（默认目标）────────────────────────────────────────
.PHONY: help
help:
	@echo "MusicTidy — Make 任务："
	@echo ""
	@echo "  make deploy           push (如未 push) + 拉 + 重启 + 健康检查"
	@echo "  make deploy-status    demo 服务状态 / cloudflared 状态"
	@echo "  make deploy-logs      tail demo 服务日志 (Ctrl-C 退出)"
	@echo "  make deploy-shell     ssh 进 demo 主机"
	@echo "  make deploy-health    单独跑一次 /healthz"
	@echo "  make deploy-rollback  demo 回滚到上一个 commit"
	@echo ""
	@echo "  make ios-build        iOS 构建 + 装到模拟器"
	@echo ""
	@echo "  make site-install     首次：site/ 装 Astro 依赖"
	@echo "  make site-dev         本地起 site dev server (4321 端口)"
	@echo "  make site-build       构建 site → site/dist/，可拖到 CF Pages"
	@echo ""

# ─── deploy ──────────────────────────────────────────────────
# push 已经在 main 上的 commits（如果还没 push）→ ssh 到 VPS 拉 + 重启 + 验
.PHONY: deploy
deploy: _push deploy-pull deploy-restart deploy-health
	@echo ""
	@echo "✓ deploy 完成。当前 demo commit:"
	@ssh $(DEMO_HOST) "cd $(DEMO_REPO_DIR) && git log -1 --oneline"

# 不强制要求工作区干净 —— 允许你 push 一部分先 deploy 着；但提示一下
.PHONY: _push
_push:
	@if ! git diff --quiet || ! git diff --cached --quiet; then \
		echo "⚠ 工作区有未 commit 的改动，先 push 的是 HEAD（不含未 commit 的）"; \
	fi
	@unpushed=$$(git log @{u}..HEAD --oneline 2>/dev/null | wc -l | tr -d ' '); \
	if [ "$$unpushed" != "0" ]; then \
		echo "→ push $$unpushed 个 commit 到 origin"; \
		git push; \
	fi

.PHONY: deploy-pull
deploy-pull:
	@echo "→ pull on demo VPS"
	@ssh $(DEMO_HOST) "cd $(DEMO_REPO_DIR) && git fetch origin && git reset --hard origin/$(DEMO_BRANCH)"
	@ssh $(DEMO_HOST) "cd $(DEMO_REPO_DIR)/server && ./.venv/bin/pip install --quiet -e . 2>&1 | tail -3 || true"

.PHONY: deploy-restart
deploy-restart:
	@echo "→ restart musictidy.service"
	@ssh $(DEMO_HOST) "sudo systemctl restart musictidy"

.PHONY: deploy-health
deploy-health:
	@echo "→ 等待 healthz..."
	@for i in 1 2 3 4 5 6 7 8 9 10; do \
		body=$$(curl -s -m 5 $(DEMO_HEALTH_URL) || true); \
		if echo "$$body" | grep -q '"app":"MusicTidy"'; then \
			echo "  ✓ healthz OK: $$body"; \
			exit 0; \
		fi; \
		echo "  ($$i/10) 还没就绪…"; \
		sleep 2; \
	done; \
	echo "  ✗ healthz 超时"; exit 1

.PHONY: deploy-status
deploy-status:
	@ssh $(DEMO_HOST) "sudo systemctl status musictidy --no-pager | head -14 && echo && sudo systemctl status cloudflared --no-pager | head -8"

.PHONY: deploy-logs
deploy-logs:
	@ssh -t $(DEMO_HOST) "sudo journalctl -u musictidy -f --no-pager"

.PHONY: deploy-shell
deploy-shell:
	@ssh $(DEMO_HOST)

# 救命用 —— 回滚一个 commit 然后重启
.PHONY: deploy-rollback
deploy-rollback:
	@echo "⚠ 把 demo 回滚到 HEAD~1，3 秒内 Ctrl-C 取消"
	@sleep 3
	@ssh $(DEMO_HOST) "cd $(DEMO_REPO_DIR) && git reset --hard HEAD~1 && git log -1 --oneline"
	@ssh $(DEMO_HOST) "sudo systemctl restart musictidy"
	@$(MAKE) deploy-health

# ─── site (Astro 静态站) ─────────────────────────────────────
.PHONY: site-install
site-install:
	@cd site && npm install

.PHONY: site-dev
site-dev:
	@cd site && npm run dev

.PHONY: site-build
site-build:
	@cd site && npm run build

# site 部署：build + wrangler pages deploy
# 需要 ~/.musictidy-cf.env 里写：
#   export CLOUDFLARE_API_TOKEN=...
#   export CLOUDFLARE_ACCOUNT_ID=...
# token 权限：Account.Cloudflare Pages: Edit + User.User Details: Read
.PHONY: site-deploy
site-deploy: site-build
	@. ~/.musictidy-cf.env && \
	wrangler pages deploy site/dist/ \
		--project-name=musictidy-site \
		--commit-dirty=true \
		2>&1 | tail -10

# ─── release sync to public repo ─────────────────────────────
# 把 dev main HEAD 的可公开子集（server/ site/ docs/ + root OSS 文件）squash 同步到
# 公开 repo WanpengQian/musictidy。每次只产生 1 个 release commit；公开 repo
# 看不到 dev 的日常 history、wip 分支、ios/ 目录。
#
# 首次准备：手动在 GitHub 建好空的 WanpengQian/musictidy（public, 不勾任何初始化）。
PUBLIC_REPO     := https://github.com/WanpengQian/musictidy.git
PUBLIC_WORKTREE := /tmp/musictidy-public
RELEASE_PATHS   := server site docs LICENSE README.md CONTRIBUTING.md .env.example .github .gitignore Makefile

.PHONY: release
release:
	@if ! git diff --quiet || ! git diff --cached --quiet; then \
		echo "✗ 工作区有未 commit 的改动，先 commit + push 再 release"; exit 1; \
	fi
	@unpushed=$$(git log @{u}..HEAD --oneline 2>/dev/null | wc -l | tr -d ' '); \
	if [ "$$unpushed" != "0" ]; then \
		echo "⚠ dev 还有 $$unpushed 个未推 commit，自动 push..."; \
		git push; \
	fi
	@DEV_SHA=$$(git rev-parse --short HEAD); \
	echo "→ release dev@$$DEV_SHA → public musictidy"; \
	rm -rf $(PUBLIC_WORKTREE); \
	echo "→ clone public repo 到 worktree"; \
	git clone --depth 1 $(PUBLIC_REPO) $(PUBLIC_WORKTREE) 2>&1 | tail -3 || { \
		echo "⚠ public repo 还空？init 一份"; \
		mkdir -p $(PUBLIC_WORKTREE); \
		cd $(PUBLIC_WORKTREE) && git init -b main && \
		git remote add origin $(PUBLIC_REPO); \
	}; \
	echo "→ 清 worktree 当前内容（保留 .git）"; \
	cd $(PUBLIC_WORKTREE) && find . -mindepth 1 -name .git -prune -o -exec rm -rf {} + 2>/dev/null || true; \
	echo "→ 从 dev 抽 release 子集"; \
	cd /Users/bendany/Developer/MusicTidy && \
	git archive HEAD -- $(RELEASE_PATHS) | tar -x -C $(PUBLIC_WORKTREE); \
	echo "→ 删 ios-build 等 dev-only Makefile target"; \
	sed -i '' '/^# ─── iOS 构建/,/^# ─── site/{/^# ─── site/!d;}' $(PUBLIC_WORKTREE)/Makefile 2>/dev/null || \
	sed -i '/^# ─── iOS 构建/,/^# ─── site/{/^# ─── site/!d;}' $(PUBLIC_WORKTREE)/Makefile; \
	echo "→ commit + push"; \
	cd $(PUBLIC_WORKTREE) && git add -A && \
	git -c user.name="Wanpeng Qian" -c user.email="support@musictidy.com" \
	    commit -m "Release sync from dev $$DEV_SHA" 2>&1 | tail -3 && \
	git push origin main --force-with-lease 2>&1 | tail -3; \
	echo ""; \
	echo "✓ release 完成。public commit:"; \
	cd $(PUBLIC_WORKTREE) && git log -1 --oneline; \
	echo ""; \
	echo "→ 清理 worktree"; \
	rm -rf $(PUBLIC_WORKTREE)
