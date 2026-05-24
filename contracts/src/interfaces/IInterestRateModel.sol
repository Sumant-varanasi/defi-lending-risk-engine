// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title IInterestRateModel
/// @notice Computes borrow and supply rates from pool utilization.
/// @dev    All rates returned in RAY (1e27), as PER-SECOND rates.
///         Annualize off-chain by multiplying by SECONDS_PER_YEAR.
interface IInterestRateModel {
    function getBorrowRatePerSecond(uint256 utilizationWad) external view returns (uint256 rateRay);

    function getSupplyRatePerSecond(uint256 utilizationWad, uint256 reserveFactorWad)
        external
        view
        returns (uint256 rateRay);
}
