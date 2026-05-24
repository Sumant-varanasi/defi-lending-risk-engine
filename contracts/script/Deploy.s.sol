// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Script.sol";
import "../src/LendingPool.sol";
import "../src/InterestRateModel.sol";
import "../src/PriceOracle.sol";
import "../src/mocks/MockERC20.sol";

/// @notice Deploy script for local Anvil or testnets.
///         Outputs a JSON snippet of addresses to stdout for the Python side to consume.
contract Deploy is Script {
    function run() external {
        uint256 pk = vm.envUint("PRIVATE_KEY");
        address deployer = vm.addr(pk);

        vm.startBroadcast(pk);

        PriceOracle oracle = new PriceOracle(deployer);
        InterestRateModel irm = new InterestRateModel(
            0.02e18,   // 2% base
            0.04e18,   // 4% slope1
            0.75e18,   // 75% slope2
            0.80e18    // 80% kink
        );
        LendingPool pool = new LendingPool(deployer, address(oracle));

        MockERC20 weth = new MockERC20("Wrapped Ether", "WETH", 18);
        MockERC20 usdc = new MockERC20("USD Coin", "USDC", 6);
        MockERC20 wbtc = new MockERC20("Wrapped Bitcoin", "WBTC", 8);

        oracle.setPrice(address(weth), 2000e18);
        oracle.setPrice(address(usdc), 1e18);
        oracle.setPrice(address(wbtc), 60000e18);

        pool.initReserve(address(weth), address(irm));
        pool.initReserve(address(usdc), address(irm));
        pool.initReserve(address(wbtc), address(irm));

        // (ltvBps, liqThresholdBps, liqBonusBps, reserveFactorBps)
        pool.configureReserve(address(weth), 7500, 8000, 500, 1000);
        pool.configureReserve(address(usdc), 8500, 8800, 500, 1000);
        pool.configureReserve(address(wbtc), 7000, 7500, 750, 2000);

        // Treasury defaults to deployer; flash loan fee defaults to 9bps.
        // Override here if you want different.
        // pool.setTreasury(otherAddress);
        // pool.setFlashLoanFeeBps(15);

        // Seed deployer with some tokens
        weth.mint(deployer, 1000e18);
        usdc.mint(deployer, 10_000_000e6);
        wbtc.mint(deployer, 100e8);

        vm.stopBroadcast();

        // ---- Emit JSON for Python config ---------------------------------
        console2.log("---DEPLOYMENT---");
        console2.log("ORACLE:", address(oracle));
        console2.log("IRM:", address(irm));
        console2.log("POOL:", address(pool));
        console2.log("WETH:", address(weth));
        console2.log("USDC:", address(usdc));
        console2.log("WBTC:", address(wbtc));
        console2.log("DEPLOYER:", deployer);
        console2.log("TREASURY:", pool.treasury());
        console2.log("FLASH_FEE_BPS:", pool.flashLoanFeeBps());
    }
}
