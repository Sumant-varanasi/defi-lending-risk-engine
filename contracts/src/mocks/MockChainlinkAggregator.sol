// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "../ChainlinkPriceOracle.sol";

/// @notice Test-only mock that implements AggregatorV3Interface.
contract MockChainlinkAggregator is AggregatorV3Interface {
    uint8 private immutable _decimals;
    int256 private _answer;
    uint256 private _updatedAt;
    uint80 private _roundId;

    string public override description = "mock";

    constructor(uint8 dec, int256 initialAnswer) {
        _decimals = dec;
        _answer = initialAnswer;
        _updatedAt = block.timestamp;
        _roundId = 1;
    }

    function decimals() external view override returns (uint8) { return _decimals; }

    function setAnswer(int256 a) external {
        _answer = a;
        _updatedAt = block.timestamp;
        _roundId += 1;
    }

    /// @notice Advance the round without updating the timestamp — simulates stale data.
    function setStale(uint256 ago) external {
        _updatedAt = block.timestamp - ago;
    }

    function latestRoundData()
        external
        view
        override
        returns (
            uint80 roundId,
            int256 answer,
            uint256 startedAt,
            uint256 updatedAt,
            uint80 answeredInRound
        )
    {
        return (_roundId, _answer, _updatedAt, _updatedAt, _roundId);
    }
}
