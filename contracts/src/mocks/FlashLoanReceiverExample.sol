// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "../interfaces/IERC20.sol";
import "../interfaces/IFlashLoanReceiver.sol";

/// @title FlashLoanReceiverExample
/// @notice Reference implementation. Demonstrates the approve-during-callback
///         pattern. Production receivers would do something useful (arbitrage,
///         liquidation, collateral swap) inside `onFlashLoan` before approving.
contract FlashLoanReceiverExample is IFlashLoanReceiver {
    address public immutable pool;

    /// Set to true to simulate a malicious / buggy receiver that fails to repay.
    bool public misbehave;

    event Borrowed(address asset, uint256 amount, uint256 fee);

    constructor(address _pool) {
        pool = _pool;
    }

    function setMisbehave(bool m) external {
        misbehave = m;
    }

    function onFlashLoan(
        address /* initiator */,
        address asset,
        uint256 amount,
        uint256 fee,
        bytes calldata /* data */
    ) external override {
        require(msg.sender == pool, "FlashRcvr: not pool");
        emit Borrowed(asset, amount, fee);

        // ... arbitrary logic would go here ...

        if (!misbehave) {
            // Approve pool to pull back principal + fee
            IERC20(asset).approve(pool, amount + fee);
        }
    }
}
