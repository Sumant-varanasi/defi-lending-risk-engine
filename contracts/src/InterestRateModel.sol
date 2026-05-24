// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "./libraries/WadMath.sol";
import "./interfaces/IInterestRateModel.sol";

/// @title InterestRateModel
/// @notice Two-slope kinked interest rate curve.
/// @dev Construction parameters are per-YEAR rates in WAD (e.g., 5% APR = 0.05e18).
///      All getters return PER-SECOND rates in RAY for use in compounding.
///
/// Curve:
///   if U <= U*:   r_borrow = base + slope1 * (U / U*)
///   if U >  U*:   r_borrow = base + slope1 + slope2 * (U - U*) / (1 - U*)
///
/// Supply rate:
///   r_supply = r_borrow * U * (1 - reserveFactor)
contract InterestRateModel is IInterestRateModel {
    uint256 public immutable baseRateWad;        // per-year, WAD
    uint256 public immutable slope1Wad;          // per-year, WAD
    uint256 public immutable slope2Wad;          // per-year, WAD
    uint256 public immutable optimalUtilWad;     // kink point in [0, 1e18]

    constructor(
        uint256 _baseRateWad,
        uint256 _slope1Wad,
        uint256 _slope2Wad,
        uint256 _optimalUtilWad
    ) {
        require(_optimalUtilWad > 0 && _optimalUtilWad < WadMath.WAD, "IRM: bad kink");
        baseRateWad = _baseRateWad;
        slope1Wad = _slope1Wad;
        slope2Wad = _slope2Wad;
        optimalUtilWad = _optimalUtilWad;
    }

    /// @notice Per-year borrow rate in WAD given utilization in WAD.
    function getBorrowRateAnnualWad(uint256 utilizationWad) public view returns (uint256) {
        if (utilizationWad <= optimalUtilWad) {
            return baseRateWad + (utilizationWad * slope1Wad) / optimalUtilWad;
        }
        uint256 excess = utilizationWad - optimalUtilWad;
        uint256 denom = WadMath.WAD - optimalUtilWad;
        return baseRateWad + slope1Wad + (excess * slope2Wad) / denom;
    }

    /// @inheritdoc IInterestRateModel
    function getBorrowRatePerSecond(uint256 utilizationWad) external view returns (uint256) {
        uint256 annualWad = getBorrowRateAnnualWad(utilizationWad);
        // Convert from per-year WAD to per-second RAY:
        // perSecondRay = annualWad * RAY / WAD / SECONDS_PER_YEAR
        return (annualWad * WadMath.WAD_RAY_RATIO) / WadMath.SECONDS_PER_YEAR;
    }

    /// @inheritdoc IInterestRateModel
    function getSupplyRatePerSecond(uint256 utilizationWad, uint256 reserveFactorWad)
        external
        view
        returns (uint256)
    {
        uint256 borrowAnnualWad = getBorrowRateAnnualWad(utilizationWad);
        // rate to pool = borrow * (1 - reserveFactor)
        uint256 rateToPoolAnnualWad =
            (borrowAnnualWad * (WadMath.WAD - reserveFactorWad)) / WadMath.WAD;
        // supply = rateToPool * U
        uint256 supplyAnnualWad = (rateToPoolAnnualWad * utilizationWad) / WadMath.WAD;
        return (supplyAnnualWad * WadMath.WAD_RAY_RATIO) / WadMath.SECONDS_PER_YEAR;
    }
}
