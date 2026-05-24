// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title IFlashLoanReceiver
/// @notice Callback interface implemented by flash loan borrowers.
/// @dev    Loosely based on ERC-3156, but the pool checks balance restoration
///         rather than relying on a return value, which is more robust against
///         buggy receivers that "return true" without actually repaying.
///
/// Flow:
///   1. caller invokes pool.flashLoan(receiver, asset, amount, data)
///   2. pool transfers `amount` of `asset` to `receiver`
///   3. pool calls receiver.onFlashLoan(initiator, asset, amount, fee, data)
///   4. receiver must approve pool to pull back (amount + fee) BEFORE the callback returns
///   5. pool pulls back (amount + fee) via transferFrom and verifies balance restored
interface IFlashLoanReceiver {
    /// @param initiator Account that called `flashLoan` on the pool
    /// @param asset     Token borrowed
    /// @param amount    Amount borrowed
    /// @param fee       Flash loan fee to pay back on top of `amount`
    /// @param data      Arbitrary data passed through from the caller
    function onFlashLoan(
        address initiator,
        address asset,
        uint256 amount,
        uint256 fee,
        bytes calldata data
    ) external;
}
