// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title IPriceOracle
/// @notice Abstraction over price feeds. Real deployments wrap Chainlink AggregatorV3Interface
///         with TWAP fallback. Mock implementation lives in PriceOracle.sol for tests.
/// @dev    All prices are quoted in USD with 18 decimals (i.e., 1 USD = 1e18).
interface IPriceOracle {
    /// @notice Returns the USD price of `asset`, scaled to 1e18.
    function getAssetPrice(address asset) external view returns (uint256);
}
