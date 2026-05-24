// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title WadMath
/// @notice Fixed-point math utilities. WAD = 1e18, RAY = 1e27.
/// @dev RAY is used for interest indices to retain precision over long horizons.
///      WAD is used for token amounts and ratios (e.g., LTV, prices in 1e18-denominated units).
library WadMath {
    uint256 internal constant WAD = 1e18;
    uint256 internal constant RAY = 1e27;
    uint256 internal constant WAD_RAY_RATIO = 1e9;
    uint256 internal constant SECONDS_PER_YEAR = 365 days;

    function wmul(uint256 a, uint256 b) internal pure returns (uint256) {
        return (a * b) / WAD;
    }

    function wdiv(uint256 a, uint256 b) internal pure returns (uint256) {
        require(b != 0, "WadMath: div by zero");
        return (a * WAD) / b;
    }

    function rmul(uint256 a, uint256 b) internal pure returns (uint256) {
        return (a * b) / RAY;
    }

    function rdiv(uint256 a, uint256 b) internal pure returns (uint256) {
        require(b != 0, "WadMath: div by zero");
        return (a * RAY) / b;
    }

    function wadToRay(uint256 a) internal pure returns (uint256) {
        return a * WAD_RAY_RATIO;
    }

    function rayToWad(uint256 a) internal pure returns (uint256) {
        return a / WAD_RAY_RATIO;
    }

    /// @notice Compound interest factor: (1 + r)^t via 3-term Taylor expansion.
    /// @dev    Sufficiently accurate for per-block intervals when r is small (rt << 1).
    ///         (1 + r)^t ≈ 1 + rt + (rt)^2/2 + (rt)^3/6
    /// @param  ratePerSecond Interest rate per second, in RAY.
    /// @param  secondsElapsed Time elapsed since last accrual.
    /// @return Compounding factor in RAY (i.e., RAY means "no change").
    function compoundFactor(uint256 ratePerSecond, uint256 secondsElapsed)
        internal
        pure
        returns (uint256)
    {
        if (secondsElapsed == 0) return RAY;

        uint256 expMinusOne = secondsElapsed - 1;
        uint256 expMinusTwo = secondsElapsed > 2 ? secondsElapsed - 2 : 0;

        uint256 basePowerTwo = rmul(ratePerSecond, ratePerSecond);
        uint256 basePowerThree = rmul(basePowerTwo, ratePerSecond);

        uint256 secondTerm = (secondsElapsed * expMinusOne * basePowerTwo) / 2;
        uint256 thirdTerm = (secondsElapsed * expMinusOne * expMinusTwo * basePowerThree) / 6;

        return RAY + (ratePerSecond * secondsElapsed) + secondTerm + thirdTerm;
    }
}
