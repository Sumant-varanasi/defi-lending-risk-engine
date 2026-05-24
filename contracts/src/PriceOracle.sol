// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "./interfaces/IPriceOracle.sol";

/// @title PriceOracle (Mock)
/// @notice Admin-settable price oracle for local development and tests.
///         Replace with a Chainlink-backed implementation for production.
contract PriceOracle is IPriceOracle {
    address public admin;
    mapping(address => uint256) private prices;

    event PriceUpdated(address indexed asset, uint256 priceWad);
    event AdminTransferred(address indexed oldAdmin, address indexed newAdmin);

    modifier onlyAdmin() {
        require(msg.sender == admin, "Oracle: not admin");
        _;
    }

    constructor(address _admin) {
        admin = _admin;
    }

    function setPrice(address asset, uint256 priceWad) external onlyAdmin {
        require(priceWad > 0, "Oracle: zero price");
        prices[asset] = priceWad;
        emit PriceUpdated(asset, priceWad);
    }

    function setPrices(address[] calldata assets, uint256[] calldata priceWads) external onlyAdmin {
        require(assets.length == priceWads.length, "Oracle: length mismatch");
        for (uint256 i = 0; i < assets.length; i++) {
            require(priceWads[i] > 0, "Oracle: zero price");
            prices[assets[i]] = priceWads[i];
            emit PriceUpdated(assets[i], priceWads[i]);
        }
    }

    function transferAdmin(address newAdmin) external onlyAdmin {
        require(newAdmin != address(0), "Oracle: zero admin");
        emit AdminTransferred(admin, newAdmin);
        admin = newAdmin;
    }

    function getAssetPrice(address asset) external view returns (uint256) {
        uint256 p = prices[asset];
        require(p > 0, "Oracle: price not set");
        return p;
    }
}
