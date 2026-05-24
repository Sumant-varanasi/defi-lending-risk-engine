// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "./interfaces/IPriceOracle.sol";

/// @title AggregatorV3Interface
/// @notice Minimal Chainlink price feed interface. The real one lives in
///         @chainlink/contracts; we copy the relevant signature here to avoid
///         pulling in an external dep.
interface AggregatorV3Interface {
    function decimals() external view returns (uint8);
    function description() external view returns (string memory);

    function latestRoundData()
        external
        view
        returns (
            uint80 roundId,
            int256 answer,
            uint256 startedAt,
            uint256 updatedAt,
            uint80 answeredInRound
        );
}


/// @title ChainlinkPriceOracle
/// @notice IPriceOracle implementation backed by Chainlink aggregators.
/// @dev    Each asset is mapped to a Chainlink USD price feed. Returns price
///         normalized to 1e18 regardless of the feed's native decimals.
///
/// Safety features:
///   - Reverts on stale data (configurable per-feed staleness window)
///   - Reverts on negative or zero answer (broken feed)
///   - Reverts on round answered in a previous round (incomplete data)
///   - Reverts when no feed registered for asset
///
/// Production extensions to consider:
///   - TWAP fallback (when Chainlink is degraded, use Uniswap V3 TWAP)
///   - Circuit breaker (max % price change per block)
///   - Sequencer uptime feed (for L2 deployments — Arbitrum, Optimism)
contract ChainlinkPriceOracle is IPriceOracle {
    struct FeedConfig {
        AggregatorV3Interface feed;
        uint32 staleAfterSecs;  // revert if updatedAt is older than this
        uint8 feedDecimals;
        bool registered;
    }

    address public admin;
    mapping(address => FeedConfig) public feeds;

    event FeedSet(address indexed asset, address indexed feed, uint32 staleAfterSecs);
    event AdminTransferred(address indexed oldAdmin, address indexed newAdmin);

    modifier onlyAdmin() {
        require(msg.sender == admin, "Oracle: not admin");
        _;
    }

    constructor(address _admin) {
        admin = _admin;
    }

    /// @notice Register a Chainlink feed for `asset`.
    /// @param asset            ERC20 token address (e.g., WETH)
    /// @param feed             Chainlink AggregatorV3 address for ASSET/USD
    /// @param staleAfterSecs   Max age of `updatedAt` before we revert (e.g., 3600
    ///                         for ETH, 86400 for stablecoins where it's normal
    ///                         for the feed to update less often)
    function setFeed(address asset, address feed, uint32 staleAfterSecs) external onlyAdmin {
        require(feed != address(0) && asset != address(0), "Oracle: zero");
        require(staleAfterSecs > 0, "Oracle: stale window");

        AggregatorV3Interface agg = AggregatorV3Interface(feed);
        uint8 decs = agg.decimals();
        require(decs > 0 && decs <= 18, "Oracle: bad feed decimals");

        feeds[asset] = FeedConfig({
            feed: agg,
            staleAfterSecs: staleAfterSecs,
            feedDecimals: decs,
            registered: true
        });
        emit FeedSet(asset, feed, staleAfterSecs);
    }

    function transferAdmin(address newAdmin) external onlyAdmin {
        require(newAdmin != address(0), "Oracle: zero admin");
        emit AdminTransferred(admin, newAdmin);
        admin = newAdmin;
    }

    /// @inheritdoc IPriceOracle
    function getAssetPrice(address asset) external view returns (uint256) {
        FeedConfig memory cfg = feeds[asset];
        require(cfg.registered, "Oracle: no feed");

        (
            uint80 roundId,
            int256 answer,
            ,
            uint256 updatedAt,
            uint80 answeredInRound
        ) = cfg.feed.latestRoundData();

        require(answer > 0, "Oracle: bad answer");
        require(updatedAt != 0, "Oracle: incomplete round");
        require(answeredInRound >= roundId, "Oracle: stale round");
        require(block.timestamp - updatedAt <= cfg.staleAfterSecs, "Oracle: stale price");

        // Normalize to 1e18
        uint256 raw = uint256(answer);
        if (cfg.feedDecimals < 18) {
            return raw * (10 ** (18 - cfg.feedDecimals));
        } else if (cfg.feedDecimals > 18) {
            return raw / (10 ** (cfg.feedDecimals - 18));
        }
        return raw;
    }
}
