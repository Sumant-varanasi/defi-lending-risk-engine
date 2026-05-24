// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Script.sol";
import "../src/LendingPool.sol";
import "../src/InterestRateModel.sol";
import "../src/ChainlinkPriceOracle.sol";
import "../src/mocks/MockERC20.sol";

/// @notice Testnet deploy. Uses ChainlinkPriceOracle wired to real Chainlink
///         feeds. Update the feed addresses below for your target network.
///         Defaults below are for **Ethereum mainnet** — replace with Sepolia
///         or whatever testnet you're deploying to.
///
/// Sepolia ETH/USD feed: 0x694AA1769357215DE4FAC081bf1f309aDC325306
/// Sepolia USDC/USD feed: 0xA2F78ab2355fe2f984D808B5CeE7FD0A93D5270E
/// Mainnet ETH/USD:       0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419
/// Mainnet USDC/USD:      0x8fFfFfd4AfB6115b954Bd326cbe7B4BA576818f6
contract TestnetDeploy is Script {
    // Sepolia defaults — override with env vars if needed
    address constant ETH_USD_FEED = 0x694AA1769357215DE4FAC081bf1f309aDC325306;
    address constant USDC_USD_FEED = 0xA2F78ab2355fe2f984D808B5CeE7FD0A93D5270E;

    function run() external {
        uint256 pk = vm.envUint("PRIVATE_KEY");
        address deployer = vm.addr(pk);

        vm.startBroadcast(pk);

        ChainlinkPriceOracle oracle = new ChainlinkPriceOracle(deployer);
        InterestRateModel irm = new InterestRateModel(
            0.02e18, 0.04e18, 0.75e18, 0.80e18
        );
        LendingPool pool = new LendingPool(deployer, address(oracle));

        MockERC20 weth = new MockERC20("Wrapped Ether", "WETH", 18);
        MockERC20 usdc = new MockERC20("USD Coin", "USDC", 6);

        // Wire feeds (3600s staleness for ETH, 86400s for stablecoins)
        oracle.setFeed(address(weth), ETH_USD_FEED, 3600);
        oracle.setFeed(address(usdc), USDC_USD_FEED, 86400);

        pool.initReserve(address(weth), address(irm));
        pool.initReserve(address(usdc), address(irm));
        pool.configureReserve(address(weth), 7500, 8000, 500, 1000);
        pool.configureReserve(address(usdc), 8500, 8800, 500, 1000);

        weth.mint(deployer, 1000e18);
        usdc.mint(deployer, 10_000_000e6);

        vm.stopBroadcast();

        console2.log("---TESTNET DEPLOYMENT---");
        console2.log("ORACLE (Chainlink):", address(oracle));
        console2.log("POOL:", address(pool));
        console2.log("WETH:", address(weth));
        console2.log("USDC:", address(usdc));
        console2.log("Live ETH price (1e18):", oracle.getAssetPrice(address(weth)));
    }
}
