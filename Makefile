.PHONY: help test sql sql-check e2e e2e-fast bench mermaid clean

PY ?= python3

help:
	@echo "make test       单元测试（35 个，不需要数据库）"
	@echo "make sql        重新生成 generated/*.sql"
	@echo "make sql-check  检查产物是否与规则同步（CI 用）"
	@echo "make e2e        端到端交叉验证 + 基准（需要 MySQL/MariaDB）"
	@echo "make e2e-fast   只跑端到端正确性"
	@echo "make bench      只跑基准，规模可调 BENCH_USERS=200,1000,4000"
	@echo "make mermaid    用真正的 mermaid 解析器校验文档里的图（需要 node）"
	@echo "make clean      清理 __pycache__ 等临时文件"
	@echo ""
	@echo "数据库连接用环境变量：MYSQL_SOCKET 或 MYSQL_HOST/PORT/USER/PASSWORD"

test:
	$(PY) -m unittest discover -s tests -p "test_*.py" -v

sql:
	@mkdir -p generated
	@for f in rules/*.dsl; do \
		b=$$(basename $$f .dsl); \
		$(PY) dslc.py $$f --out generated/$$b.sql; \
	done
	@$(PY) dslc.py rules/r1_original.dsl --tier 2 --out generated/r1_original.forced_tier2.sql
	@echo "generated/ 已更新"

sql-check:
	@$(PY) -m unittest discover -s tests -p "test_compiler.py" \
		-k generated_artifacts -v

e2e:
	$(PY) tests/e2e_verify.py

e2e-fast:
	$(PY) tests/e2e_verify.py --skip-bench

BENCH_USERS ?= 200,1000,4000
bench:
	$(PY) tests/e2e_verify.py --bench-users $(BENCH_USERS)

mermaid:
	cd tools && npm install --silent --no-save mermaid jsdom \
		&& node check_mermaid.mjs ../README.md ../docs/DESIGN.md ../docs/DSL.md ../docs/BENCHMARKS.md

clean:
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find . -name '*.pyc' -delete
	rm -rf .pytest_cache .mypy_cache
