// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import "../src/LendingPool.sol";
import "../src/InterestRateModel.sol";
import "../src/PriceOracle.sol";
import "../src/mocks/MockERC20.sol";
import "../src/mocks/FlashLoanReceiverExample.sol";

contract FlashLoanAndTreasuryTest is Test {
    LendingPool pool;
    InterestRateModel irm;
    PriceOracle oracle;
    MockERC20 weth;
    MockERC20 usdc;

    address admin = address(0xA11CE);
    address alice = address(0xA1);
    address bob   = address(0xB0B);
    address treasuryRecipient = address(0x77777);

    function setUp() public {
        vm.startPrank(admin);
        oracle = new PriceOracle(admin);
        pool = new LendingPool(admin, address(oracle));
        irm = new InterestRateModel(0.02e18, 0.04e18, 0.75e18, 0.80e18);

        weth = new MockERC20("Wrapped Ether", "WETH", 18);
        usdc = new MockERC20("USD Coin", "USDC", 6);

        oracle.setPrice(address(weth), 2000e18);
        oracle.setPrice(address(usdc), 1e18);

        pool.initReserve(address(weth), address(irm));
        pool.initReserve(address(usdc), address(irm));
        pool.configureReserve(address(weth), 7500, 8000, 500, 1000);
        pool.configureReserve(address(usdc), 8500, 8800, 500, 1000);

        pool.setTreasury(treasuryRecipient);
        vm.stopPrank();

        weth.mint(alice, 100e18);
        usdc.mint(alice, 1_000_000e6);
        weth.mint(bob, 10e18);

        vm.prank(alice); weth.approve(address(pool), type(uint256).max);
        vm.prank(alice); usdc.approve(address(pool), type(uint256).max);
        vm.prank(bob);   weth.approve(address(pool), type(uint256).max);
        vm.prank(bob);   usdc.approve(address(pool), type(uint256).max);
    }

    // ------------------------------------------------------------------
    // Treasury accrual
    // ------------------------------------------------------------------
    function test_treasuryAccrues_overTime() public {
        // Set up borrowing activity so reserveFactor takes effect
        vm.prank(alice); pool.deposit(address(usdc), 100_000e6);
        vm.prank(bob);   pool.deposit(address(weth), 1e18);
        vm.prank(bob);   pool.borrow(address(usdc), 1000e6);

        vm.warp(block.timestamp + 365 days);

        // trigger accrual
        vm.prank(alice); pool.deposit(address(usdc), 1e6);

        uint256 accrued = _accruedTreasury(address(usdc));
        assertGt(accrued, 0, "treasury should have accrued");
    }

    function test_mintToTreasury_transfersToRecipient() public {
        vm.prank(alice); pool.deposit(address(usdc), 100_000e6);
        vm.prank(bob);   pool.deposit(address(weth), 1e18);
        vm.prank(bob);   pool.borrow(address(usdc), 1000e6);

        vm.warp(block.timestamp + 365 days);

        // give bob enough to repay so liquidity is available
        usdc.mint(bob, 1000e6);
        vm.prank(bob); pool.repay(address(usdc), type(uint256).max);

        uint256 balBefore = usdc.balanceOf(treasuryRecipient);
        vm.prank(admin); pool.mintToTreasury(address(usdc));
        uint256 received = usdc.balanceOf(treasuryRecipient) - balBefore;
        assertGt(received, 0, "treasury should receive USDC");

        // After minting, accrued balance is zero
        uint256 accrued = _accruedTreasury(address(usdc));
        assertEq(accrued, 0);
    }

    function test_onlyAdmin_canMintTreasury() public {
        vm.prank(alice);
        vm.expectRevert(bytes("Pool: not admin"));
        pool.mintToTreasury(address(usdc));
    }

    // ------------------------------------------------------------------
    // Flash loans
    // ------------------------------------------------------------------
    function test_flashLoan_succeeds_whenRepaid() public {
        vm.prank(alice); pool.deposit(address(usdc), 100_000e6);

        FlashLoanReceiverExample rcvr = new FlashLoanReceiverExample(address(pool));
        // Receiver needs USDC to pay the fee
        usdc.mint(address(rcvr), 1000e6);

        uint256 poolBalanceBefore = usdc.balanceOf(address(pool));
        pool.flashLoan(address(rcvr), address(usdc), 10_000e6, "");

        uint256 poolBalanceAfter = usdc.balanceOf(address(pool));
        // Fee is 0.09% by default => 10000 * 0.0009 = 9 USDC
        assertEq(poolBalanceAfter - poolBalanceBefore, 9e6, "pool gained the fee");
    }

    function test_flashLoan_reverts_whenReceiverDoesNotRepay() public {
        vm.prank(alice); pool.deposit(address(usdc), 100_000e6);

        FlashLoanReceiverExample rcvr = new FlashLoanReceiverExample(address(pool));
        rcvr.setMisbehave(true);

        vm.expectRevert();  // either "Pool: xferFrom failed" or "Pool: not repaid"
        pool.flashLoan(address(rcvr), address(usdc), 10_000e6, "");
    }

    function test_flashLoan_feeAccruesToTreasury() public {
        vm.prank(alice); pool.deposit(address(usdc), 100_000e6);

        FlashLoanReceiverExample rcvr = new FlashLoanReceiverExample(address(pool));
        usdc.mint(address(rcvr), 1000e6);

        pool.flashLoan(address(rcvr), address(usdc), 10_000e6, "");

        uint256 accrued = _accruedTreasury(address(usdc));
        assertGt(accrued, 0, "flash fee should accrue to treasury");
    }

    function test_setFlashLoanFee_caps() public {
        vm.prank(admin);
        vm.expectRevert(bytes("Pool: fee too high"));
        pool.setFlashLoanFeeBps(101);
    }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------
    function _accruedTreasury(address asset) internal view returns (uint256) {
        // ReserveData fields in order:
        // 0 liquidityIndex (uint128)
        // 1 borrowIndex (uint128)
        // 2 lastUpdateTimestamp (uint40)
        // 3 totalScaledSupply (uint256)
        // 4 totalScaledBorrow (uint256)
        // 5 accruedToTreasuryScaled (uint256)   <-- want this
        // 6..13 config + flags
        (, , , , , uint256 accrued, , , , , , , , ) = pool.reserves(asset);
        return accrued;
    }
}
