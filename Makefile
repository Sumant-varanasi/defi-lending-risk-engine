.PHONY: help install build test test-fuzz test-invariant abi deploy-local deploy-testnet anvil indexer aggregator risk-engine liquidator dashboard seed backtest mint-treasury clean

ROOT := $(shell pwd)
ABI_OUT := python/abi
CONTRACTS_OUT := contracts/out

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install:  ## Install Python dependencies
	pip install -r python/requirements.txt

build:  ## Compile Solidity contracts
	cd contracts && forge build

test:  ## Run Foundry tests (all)
	cd contracts && forge test -vvv

test-fuzz:  ## Run only fuzz tests with more runs
	cd contracts && forge test --match-contract LendingPoolFuzzTest -vv --fuzz-runs 1000

test-invariant:  ## Run invariant tests
	cd contracts && forge test --match-contract LendingPoolInvariantTest -vv

abi:  ## Extract ABIs from forge output into python/abi/
	@mkdir -p $(ABI_OUT)
	@for c in LendingPool PriceOracle InterestRateModel MockERC20 ChainlinkPriceOracle; do \
		jq '.abi' $(CONTRACTS_OUT)/$$c.sol/$$c.json > $(ABI_OUT)/$$c.json ; \
		echo "  wrote $(ABI_OUT)/$$c.json" ; \
	done

anvil:  ## Start a local Anvil node (in a separate terminal)
	anvil --block-time 2

deploy-local:  ## Deploy to local Anvil. Requires PRIVATE_KEY in .env
	cd contracts && \
	forge script script/Deploy.s.sol \
		--rpc-url http://127.0.0.1:8545 \
		--broadcast \
		--private-key $$(grep ^PRIVATE_KEY ../.env | cut -d= -f2)

deploy-testnet:  ## Deploy to Sepolia via Chainlink oracle. Set PRIVATE_KEY + RPC_URL in .env
	cd contracts && \
	forge script script/TestnetDeploy.s.sol \
		--rpc-url $$(grep ^RPC_URL ../.env | cut -d= -f2) \
		--broadcast \
		--private-key $$(grep ^PRIVATE_KEY ../.env | cut -d= -f2)

indexer:  ## Run the event indexer service
	PYTHONPATH=$(ROOT) python -m python.liquidator.event_indexer

aggregator:  ## Run the periodic state snapshotter
	PYTHONPATH=$(ROOT) python -m python.analytics.aggregator

risk-engine:  ## Run the periodic risk recommender
	PYTHONPATH=$(ROOT) python -m python.risk_engine.runner

liquidator:  ## Run the liquidator bot
	PYTHONPATH=$(ROOT) python -m python.liquidator.bot

dashboard:  ## Launch the Streamlit dashboard on :8501
	PYTHONPATH=$(ROOT) streamlit run python/dashboard/app.py

seed:  ## Seed demo positions (run AFTER deploy and copying addresses to .env)
	PYTHONPATH=$(ROOT) python scripts/seed_demo.py

backtest:  ## Run the LTV-policy backtest. Outputs into backtest_results/
	PYTHONPATH=$(ROOT) python -m python.backtest.run

mint-treasury:  ## Mint accrued treasury for an asset. Requires ASSET=0x... in env
	cast send --private-key $$(grep ^PRIVATE_KEY .env | cut -d= -f2) \
		--rpc-url http://127.0.0.1:8545 \
		$$(grep ^POOL_ADDR .env | cut -d= -f2) \
		"mintToTreasury(address)" $(ASSET)

clean:  ## Remove build artifacts and DB
	rm -rf contracts/out contracts/cache python/abi/*.json data/*.sqlite* backtest_results
	find . -type d -name __pycache__ -exec rm -rf {} +
