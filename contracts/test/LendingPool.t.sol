// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import "../src/LendingPool.sol";
import "../src/InterestRateModel.sol";
import "../src/PriceOracle.sol";
import "../src/mocks/MockERC20.sol";
import "../src/libraries/WadMath.sol";

contract LendingPoolTest is Test {
    LendingPool pool;
    InterestRateModel irm;
    PriceOracle oracle;

    MockERC20 weth;   // 18 decimals, ~$2000
    MockERC20 usdc;   // 6 decimals, $1.00

    address admin = address(0xA11CE);
    address alice = address(0xA1);   // supplier
    address bob   = address(0xB0B);  // borrower
    address liq   = address(0x111D); // liquidator

    uint256 constant WAD = 1e18;
    uint256 constant RAY = 1e27;

    function setUp() public {
        vm.startPrank(admin);

        oracle = new PriceOracle(admin);
        pool = new LendingPool(admin, address(oracle));

        // 2% base, 4% slope1, 75% slope2, 80% kink
        irm = new InterestRateModel(0.02e18, 0.04e18, 0.75e18, 0.80e18);

        weth = new MockERC20("Wrapped Ether", "WETH", 18);
        usdc = new MockERC20("USD Coin", "USDC", 6);

        oracle.setPrice(address(weth), 2000e18);
        oracle.setPrice(address(usdc), 1e18);

        pool.initReserve(address(weth), address(irm));
        pool.initReserve(address(usdc), address(irm));

        // WETH: 75% LTV, 80% liq threshold, 5% bonus, 10% reserve factor
        pool.configureReserve(address(weth), 7500, 8000, 500, 1000);
        // USDC: 85% LTV, 88% liq threshold, 5% bonus, 10% reserve factor
        pool.configureReserve(address(usdc), 8500, 8800, 500, 1000);

        vm.stopPrank();

        // Fund actors
        weth.mint(alice, 100e18);
        weth.mint(bob, 10e18);
        usdc.mint(alice, 1_000_000e6);
        usdc.mint(liq, 1_000_000e6);

        vm.prank(alice);  weth.approve(address(pool), type(uint256).max);
        vm.prank(alice);  usdc.approve(address(pool), type(uint256).max);
        vm.prank(bob);    weth.approve(address(pool), type(uint256).max);
        vm.prank(bob);    usdc.approve(address(pool), type(uint256).max);
        vm.prank(liq);    usdc.approve(address(pool), type(uint256).max);
        vm.prank(liq);    weth.approve(address(pool), type(uint256).max);
    }

    // -----------------------------------------------------------------
    // Deposit / Withdraw
    // -----------------------------------------------------------------
    function test_deposit_increasesScaledBalance() public {
        vm.prank(alice);
        pool.deposit(address(weth), 10e18);

        (uint256 supplied, , ) = pool.getUserReserveData(alice, address(weth));
        assertEq(supplied, 10e18);
    }

    function test_withdraw_returnsFunds() public {
        vm.prank(alice);
        pool.deposit(address(weth), 10e18);

        uint256 balBefore = weth.balanceOf(alice);
        vm.prank(alice);
        pool.withdraw(address(weth), 4e18);
        assertEq(weth.balanceOf(alice) - balBefore, 4e18);
    }

    // -----------------------------------------------------------------
    // Borrow / Repay
    // -----------------------------------------------------------------
    function test_borrow_respectsLtv() public {
        // Alice supplies USDC liquidity
        vm.prank(alice);
        pool.deposit(address(usdc), 100_000e6);

        // Bob deposits 1 WETH ($2000) as collateral, tries to borrow 1500 USDC = max LTV
        vm.prank(bob);
        pool.deposit(address(weth), 1e18);

        vm.prank(bob);
        pool.borrow(address(usdc), 1500e6); // 75% of 2000 = 1500 -> exactly at LTV cap

        ( , , , , ,uint256 hf) = pool.getUserAccountData(bob);
        // HF = 2000 * 0.8 / 1500 = 1.0667
        assertGt(hf, WAD);
        assertLt(hf, 1.1e18);
    }

    function test_borrow_revertsAboveLtv() public {
        vm.prank(alice);
        pool.deposit(address(usdc), 100_000e6);
        vm.prank(bob);
        pool.deposit(address(weth), 1e18);

        vm.prank(bob);
        vm.expectRevert(bytes("Pool: HF < 1"));
        pool.borrow(address(usdc), 1700e6); // > 75% LTV
    }

    function test_repay_clearsDebt() public {
        vm.prank(alice);  pool.deposit(address(usdc), 100_000e6);
        vm.prank(bob);    pool.deposit(address(weth), 1e18);
        vm.prank(bob);    pool.borrow(address(usdc), 1000e6);

        // give bob enough usdc to repay + interest
        usdc.mint(bob, 100e6);

        vm.prank(bob);
        pool.repay(address(usdc), type(uint256).max);

        ( , uint256 borrowed, ) = pool.getUserReserveData(bob, address(usdc));
        assertEq(borrowed, 0);
    }

    // -----------------------------------------------------------------
    // Interest accrual
    // -----------------------------------------------------------------
    function test_interestAccrues() public {
        vm.prank(alice);  pool.deposit(address(usdc), 100_000e6);
        vm.prank(bob);    pool.deposit(address(weth), 1e18);
        vm.prank(bob);    pool.borrow(address(usdc), 1000e6);

        ( , uint256 borrowedBefore, ) = pool.getUserReserveData(bob, address(usdc));

        // Advance 1 year
        vm.warp(block.timestamp + 365 days);

        ( , uint256 borrowedAfter, ) = pool.getUserReserveData(bob, address(usdc));
        // utilization is 1%, so borrow rate is roughly base (2%) + (1/80)*4% ≈ 2.05% APR
        // expect borrowedAfter ≈ 1020.5 USDC
        assertGt(borrowedAfter, borrowedBefore);
        assertGt(borrowedAfter, 1015e6);
        assertLt(borrowedAfter, 1030e6);
    }

    // -----------------------------------------------------------------
    // Liquidation
    // -----------------------------------------------------------------
    function test_liquidation_priceDrop() public {
        // Alice supplies 100k USDC. Bob deposits 1 WETH @ $2000, borrows 1400 USDC (HF ~ 1.143)
        vm.prank(alice);  pool.deposit(address(usdc), 100_000e6);
        vm.prank(bob);    pool.deposit(address(weth), 1e18);
        vm.prank(bob);    pool.borrow(address(usdc), 1400e6);

        // ETH crashes to $1500 -> collateral value 1500, debt 1400
        // HF = 1500 * 0.8 / 1400 = 0.857 -> liquidatable
        vm.prank(admin);
        oracle.setPrice(address(weth), 1500e18);

        ( , , , , , uint256 hfBefore) = pool.getUserAccountData(bob);
        assertLt(hfBefore, WAD);

        // Liquidator covers half the debt (closeFactor = 50%)
        uint256 wethBefore = weth.balanceOf(liq);
        vm.prank(liq);
        pool.liquidate(bob, address(weth), address(usdc), 700e6);

        // Liquidator should have received WETH worth ~700 + 5% bonus = $735 of ETH = 0.49 WETH
        uint256 wethReceived = weth.balanceOf(liq) - wethBefore;
        // 735 / 1500 = 0.49 WETH
        assertApproxEqRel(wethReceived, 0.49e18, 0.01e18);
    }

    function test_liquidation_revertsIfHealthy() public {
        vm.prank(alice);  pool.deposit(address(usdc), 100_000e6);
        vm.prank(bob);    pool.deposit(address(weth), 1e18);
        vm.prank(bob);    pool.borrow(address(usdc), 1000e6);

        vm.prank(liq);
        vm.expectRevert(bytes("Pool: healthy"));
        pool.liquidate(bob, address(weth), address(usdc), 500e6);
    }

    // -----------------------------------------------------------------
    // Utilization & rate curve
    // -----------------------------------------------------------------
    function test_borrowRateBelowKink() public {
        // 0% utilization -> base rate only
        uint256 r = irm.getBorrowRateAnnualWad(0);
        assertEq(r, 0.02e18);
    }

    function test_borrowRateAtKink() public {
        // 80% utilization -> base + slope1 = 6%
        uint256 r = irm.getBorrowRateAnnualWad(0.80e18);
        assertEq(r, 0.06e18);
    }

    function test_borrowRateAboveKink() public {
        // 100% utilization -> base + slope1 + slope2 = 2 + 4 + 75 = 81%
        uint256 r = irm.getBorrowRateAnnualWad(WAD);
        assertEq(r, 0.81e18);
    }
}
