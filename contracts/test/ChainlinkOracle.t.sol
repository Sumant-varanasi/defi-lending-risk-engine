// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import "../src/ChainlinkPriceOracle.sol";
import "../src/mocks/MockChainlinkAggregator.sol";

contract ChainlinkOracleTest is Test {
    ChainlinkPriceOracle oracle;
    MockChainlinkAggregator ethFeed;
    MockChainlinkAggregator usdcFeed;

    address admin = address(0xA11CE);
    address weth = address(0x1111);
    address usdc = address(0x2222);

    function setUp() public {
        vm.warp(1_700_000_000);  // some realistic timestamp
        vm.prank(admin);
        oracle = new ChainlinkPriceOracle(admin);

        // ETH/USD typically has 8 decimals on mainnet; $2000 = 2000e8
        ethFeed = new MockChainlinkAggregator(8, 2000e8);
        // USDC/USD also 8 decimals; $1 = 1e8
        usdcFeed = new MockChainlinkAggregator(8, 1e8);

        vm.startPrank(admin);
        oracle.setFeed(weth, address(ethFeed), 3600);
        oracle.setFeed(usdc, address(usdcFeed), 86400);
        vm.stopPrank();
    }

    function test_returnsNormalizedPrice() public {
        // 8-decimal feed @ 2000e8 should normalize to 2000e18
        uint256 ethPrice = oracle.getAssetPrice(weth);
        assertEq(ethPrice, 2000e18);

        uint256 usdcPrice = oracle.getAssetPrice(usdc);
        assertEq(usdcPrice, 1e18);
    }

    function test_revertsOnStalePrice() public {
        // Mark ETH feed as 2 hours stale (window is 1 hour)
        ethFeed.setStale(2 * 3600);

        vm.expectRevert(bytes("Oracle: stale price"));
        oracle.getAssetPrice(weth);
    }

    function test_revertsOnNegativePrice() public {
        ethFeed.setAnswer(-1);
        vm.expectRevert(bytes("Oracle: bad answer"));
        oracle.getAssetPrice(weth);
    }

    function test_revertsOnZeroPrice() public {
        ethFeed.setAnswer(0);
        vm.expectRevert(bytes("Oracle: bad answer"));
        oracle.getAssetPrice(weth);
    }

    function test_revertsOnUnknownAsset() public {
        vm.expectRevert(bytes("Oracle: no feed"));
        oracle.getAssetPrice(address(0xdead));
    }

    function test_onlyAdmin_canSetFeed() public {
        vm.expectRevert(bytes("Oracle: not admin"));
        oracle.setFeed(weth, address(ethFeed), 3600);
    }

    function test_updatedPrice_propagates() public {
        // ETH crashes to $1500
        ethFeed.setAnswer(1500e8);
        assertEq(oracle.getAssetPrice(weth), 1500e18);
    }
}
