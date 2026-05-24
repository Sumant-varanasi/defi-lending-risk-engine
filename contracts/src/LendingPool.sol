// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "./libraries/WadMath.sol";
import "./interfaces/IERC20.sol";
import "./interfaces/IPriceOracle.sol";
import "./interfaces/IInterestRateModel.sol";
import "./interfaces/IFlashLoanReceiver.sol";

/// @title LendingPool
/// @notice Multi-asset lending pool with kinked interest rates, scaled balances, and
///         permissionless liquidations. Inspired by Aave V2 + Compound V2.
///
/// @dev    Key invariants:
///         - scaledBalance × index = actual balance (denominated in token native units)
///         - liquidityIndex monotonically increases, indexed in RAY (1e27)
///         - borrowIndex monotonically increases, indexed in RAY (1e27)
///         - Health Factor (HF) = (Σ collateral_i × liqThreshold_i) / Σ debt_i
///         - When HF < 1e18, position is liquidatable
contract LendingPool {
    using WadMath for uint256;

    // ---- constants -----------------------------------------------------
    uint256 private constant BPS = 10_000;
    uint256 private constant HF_LIQUIDATION_THRESHOLD = WadMath.WAD; // 1.0
    uint256 private constant CLOSE_FACTOR_BPS = 5_000;               // liquidator may seize 50% of debt per call
    uint256 private constant MAX_LIQUIDATION_BONUS_BPS = 1_500;      // 15% cap

    // ---- types ---------------------------------------------------------
    struct ReserveData {
        uint128 liquidityIndex;          // RAY
        uint128 borrowIndex;             // RAY
        uint40  lastUpdateTimestamp;

        uint256 totalScaledSupply;       // sum of all users' scaled deposits
        uint256 totalScaledBorrow;       // sum of all users' scaled debt
        uint256 accruedToTreasuryScaled; // scaled aToken-equivalent accrued to treasury

        // configuration (basis points / WAD)
        uint16  ltvBps;                  // max borrow as % of this collateral (e.g., 7500)
        uint16  liquidationThresholdBps; // HF=1 trigger threshold (e.g., 8000)
        uint16  liquidationBonusBps;     // discount given to liquidator (e.g., 500)
        uint16  reserveFactorBps;        // % of borrow interest skimmed to treasury

        address interestRateModel;
        bool    active;
        bool    borrowEnabled;
        bool    usableAsCollateral;
    }

    struct UserData {
        uint256 scaledSupply;
        uint256 scaledBorrow;
        bool    useAsCollateral;         // per-user opt-out flag
    }

    // ---- storage -------------------------------------------------------
    mapping(address asset => ReserveData) public reserves;
    mapping(address user => mapping(address asset => UserData)) public users;

    address[] public reservesList;
    mapping(address asset => bool) public reserveExists;

    IPriceOracle public oracle;
    address public admin;
    address public treasury;
    uint256 public flashLoanFeeBps;  // e.g., 9 = 0.09% (Aave-style)
    bool public paused;
    uint256 private _reentrancyLock = 1;

    // ---- events --------------------------------------------------------
    event ReserveInitialized(address indexed asset, address indexed irm);
    event ReserveConfigured(
        address indexed asset,
        uint16 ltvBps,
        uint16 liqThresholdBps,
        uint16 liqBonusBps,
        uint16 reserveFactorBps
    );
    event Deposit(address indexed user, address indexed asset, uint256 amount);
    event Withdraw(address indexed user, address indexed asset, uint256 amount);
    event Borrow(address indexed user, address indexed asset, uint256 amount, uint256 borrowIndex);
    event Repay(address indexed user, address indexed asset, uint256 amount);
    event Liquidation(
        address indexed liquidator,
        address indexed borrower,
        address debtAsset,
        address collateralAsset,
        uint256 debtRepaid,
        uint256 collateralSeized
    );
    event PriceOracleSet(address oracle);
    event CollateralStatusChanged(address indexed user, address indexed asset, bool useAsCollateral);
    event TreasurySet(address treasury);
    event FlashLoanFeeSet(uint256 feeBps);
    event TreasuryMinted(address indexed asset, address indexed to, uint256 amount);
    event FlashLoan(
        address indexed receiver,
        address indexed initiator,
        address indexed asset,
        uint256 amount,
        uint256 fee
    );

    // ---- modifiers -----------------------------------------------------
    modifier onlyAdmin() {
        require(msg.sender == admin, "Pool: not admin");
        _;
    }

    modifier nonReentrant() {
        require(_reentrancyLock == 1, "Pool: reentrancy");
        _reentrancyLock = 2;
        _;
        _reentrancyLock = 1;
    }

    modifier whenNotPaused() {
        require(!paused, "Pool: paused");
        _;
    }

    constructor(address _admin, address _oracle) {
        admin = _admin;
        treasury = _admin;
        oracle = IPriceOracle(_oracle);
        flashLoanFeeBps = 9;  // 0.09% default, Aave-style
        emit PriceOracleSet(_oracle);
        emit TreasurySet(_admin);
        emit FlashLoanFeeSet(9);
    }

    // ===================================================================
    // Admin
    // ===================================================================
    function initReserve(address asset, address irm) external onlyAdmin {
        require(!reserveExists[asset], "Pool: exists");
        require(irm != address(0), "Pool: bad IRM");

        ReserveData storage r = reserves[asset];
        r.liquidityIndex = uint128(WadMath.RAY);
        r.borrowIndex = uint128(WadMath.RAY);
        r.lastUpdateTimestamp = uint40(block.timestamp);
        r.interestRateModel = irm;
        r.active = true;
        r.borrowEnabled = true;
        r.usableAsCollateral = true;

        reservesList.push(asset);
        reserveExists[asset] = true;
        emit ReserveInitialized(asset, irm);
    }

    function configureReserve(
        address asset,
        uint16 ltvBps,
        uint16 liqThresholdBps,
        uint16 liqBonusBps,
        uint16 reserveFactorBps
    ) external onlyAdmin {
        require(reserveExists[asset], "Pool: !init");
        require(ltvBps <= liqThresholdBps && liqThresholdBps <= BPS, "Pool: bad thresholds");
        require(liqBonusBps <= MAX_LIQUIDATION_BONUS_BPS, "Pool: bonus too high");
        require(reserveFactorBps < BPS, "Pool: rf too high");

        ReserveData storage r = reserves[asset];
        r.ltvBps = ltvBps;
        r.liquidationThresholdBps = liqThresholdBps;
        r.liquidationBonusBps = liqBonusBps;
        r.reserveFactorBps = reserveFactorBps;
        emit ReserveConfigured(asset, ltvBps, liqThresholdBps, liqBonusBps, reserveFactorBps);
    }

    function setInterestRateModel(address asset, address irm) external onlyAdmin {
        require(reserveExists[asset] && irm != address(0), "Pool: bad arg");
        reserves[asset].interestRateModel = irm;
    }

    function setOracle(address _oracle) external onlyAdmin {
        oracle = IPriceOracle(_oracle);
        emit PriceOracleSet(_oracle);
    }

    function setPaused(bool p) external onlyAdmin { paused = p; }

    function setTreasury(address t) external onlyAdmin {
        require(t != address(0), "Pool: zero");
        treasury = t;
        emit TreasurySet(t);
    }

    function setFlashLoanFeeBps(uint256 bps) external onlyAdmin {
        require(bps <= 100, "Pool: fee too high"); // cap at 1%
        flashLoanFeeBps = bps;
        emit FlashLoanFeeSet(bps);
    }

    /// @notice Mint accrued treasury balance for `asset` to the treasury address.
    /// @dev    The accrued amount is denominated in scaled units; we convert to
    ///         actual tokens using the current liquidity index. The pool must
    ///         hold enough liquidity to back the withdrawal.
    function mintToTreasury(address asset) external nonReentrant onlyAdmin {
        require(reserveExists[asset], "Pool: !init");
        _accrueInterest(asset);

        ReserveData storage r = reserves[asset];
        uint256 scaled = r.accruedToTreasuryScaled;
        if (scaled == 0) return;

        uint256 amount = (scaled * r.liquidityIndex) / WadMath.RAY;
        require(_availableLiquidity(asset) >= amount, "Pool: liquidity");

        r.accruedToTreasuryScaled = 0;
        _safeTransfer(asset, treasury, amount);
        emit TreasuryMinted(asset, treasury, amount);
    }

    function transferAdmin(address newAdmin) external onlyAdmin {
        require(newAdmin != address(0), "Pool: zero");
        admin = newAdmin;
    }

    // ===================================================================
    // User actions
    // ===================================================================
    function deposit(address asset, uint256 amount) external nonReentrant whenNotPaused {
        require(reserveExists[asset] && reserves[asset].active, "Pool: inactive");
        require(amount > 0, "Pool: zero amt");

        _accrueInterest(asset);

        ReserveData storage r = reserves[asset];
        UserData storage u = users[msg.sender][asset];

        // scaled = amount * RAY / liquidityIndex
        uint256 scaled = (amount * WadMath.RAY) / r.liquidityIndex;
        u.scaledSupply += scaled;
        r.totalScaledSupply += scaled;

        // first deposit -> automatically usable as collateral
        if (!u.useAsCollateral) {
            u.useAsCollateral = true;
            emit CollateralStatusChanged(msg.sender, asset, true);
        }

        _safeTransferFrom(asset, msg.sender, address(this), amount);
        emit Deposit(msg.sender, asset, amount);
    }

    function withdraw(address asset, uint256 amount) external nonReentrant whenNotPaused {
        require(reserveExists[asset], "Pool: !init");
        require(amount > 0, "Pool: zero amt");

        _accrueInterest(asset);

        ReserveData storage r = reserves[asset];
        UserData storage u = users[msg.sender][asset];

        uint256 actualSupply = (u.scaledSupply * r.liquidityIndex) / WadMath.RAY;
        require(actualSupply >= amount, "Pool: balance");

        // pool must have liquidity
        require(_availableLiquidity(asset) >= amount, "Pool: liquidity");

        uint256 scaledBurn = (amount * WadMath.RAY) / r.liquidityIndex;
        // handle dust: if user is withdrawing everything, burn all scaled
        if (amount == actualSupply) scaledBurn = u.scaledSupply;

        u.scaledSupply -= scaledBurn;
        r.totalScaledSupply -= scaledBurn;

        // post-withdraw health check (if this asset is collateral)
        if (u.useAsCollateral && _userHasDebt(msg.sender)) {
            require(_healthFactor(msg.sender) >= HF_LIQUIDATION_THRESHOLD, "Pool: would liquidate");
        }

        _safeTransfer(asset, msg.sender, amount);
        emit Withdraw(msg.sender, asset, amount);
    }

    function borrow(address asset, uint256 amount) external nonReentrant whenNotPaused {
        require(reserveExists[asset] && reserves[asset].active, "Pool: inactive");
        require(reserves[asset].borrowEnabled, "Pool: borrow disabled");
        require(amount > 0, "Pool: zero amt");
        require(_availableLiquidity(asset) >= amount, "Pool: liquidity");

        _accrueInterest(asset);

        ReserveData storage r = reserves[asset];
        UserData storage u = users[msg.sender][asset];

        uint256 scaledIncrease = (amount * WadMath.RAY) / r.borrowIndex;
        u.scaledBorrow += scaledIncrease;
        r.totalScaledBorrow += scaledIncrease;

        // Health check post-borrow
        require(_healthFactor(msg.sender) >= HF_LIQUIDATION_THRESHOLD, "Pool: HF < 1");

        _safeTransfer(asset, msg.sender, amount);
        emit Borrow(msg.sender, asset, amount, r.borrowIndex);
    }

    function repay(address asset, uint256 amount) external nonReentrant whenNotPaused returns (uint256) {
        require(reserveExists[asset], "Pool: !init");
        require(amount > 0, "Pool: zero amt");

        _accrueInterest(asset);

        ReserveData storage r = reserves[asset];
        UserData storage u = users[msg.sender][asset];

        uint256 actualDebt = (u.scaledBorrow * r.borrowIndex) / WadMath.RAY;
        require(actualDebt > 0, "Pool: no debt");

        uint256 payAmount = amount > actualDebt ? actualDebt : amount;
        uint256 scaledBurn = (payAmount * WadMath.RAY) / r.borrowIndex;
        if (payAmount == actualDebt) scaledBurn = u.scaledBorrow; // burn all to avoid dust

        u.scaledBorrow -= scaledBurn;
        r.totalScaledBorrow -= scaledBurn;

        _safeTransferFrom(asset, msg.sender, address(this), payAmount);
        emit Repay(msg.sender, asset, payAmount);
        return payAmount;
    }

    function setUseAsCollateral(address asset, bool use) external nonReentrant whenNotPaused {
        require(reserveExists[asset], "Pool: !init");
        UserData storage u = users[msg.sender][asset];
        if (u.useAsCollateral == use) return;
        u.useAsCollateral = use;

        // if disabling, must remain healthy
        if (!use && _userHasDebt(msg.sender)) {
            require(_healthFactor(msg.sender) >= HF_LIQUIDATION_THRESHOLD, "Pool: would liquidate");
        }
        emit CollateralStatusChanged(msg.sender, asset, use);
    }

    // ===================================================================
    // Liquidation
    // ===================================================================
    /// @notice Liquidate an unhealthy position. Liquidator pays `debtToCover` of `debtAsset`
    ///         in exchange for an equivalent value of `collateralAsset` plus a bonus.
    /// @dev    Reverts if HF >= 1, collateral asset isn't being used as collateral, or
    ///         debt asset isn't borrowed.
    function liquidate(
        address borrower,
        address collateralAsset,
        address debtAsset,
        uint256 debtToCover
    ) external nonReentrant whenNotPaused {
        require(borrower != msg.sender, "Pool: self");
        require(reserveExists[collateralAsset] && reserveExists[debtAsset], "Pool: !init");

        _accrueInterest(collateralAsset);
        if (debtAsset != collateralAsset) _accrueInterest(debtAsset);

        require(_healthFactor(borrower) < HF_LIQUIDATION_THRESHOLD, "Pool: healthy");

        UserData storage uBorrowerDebt = users[borrower][debtAsset];
        UserData storage uBorrowerColl = users[borrower][collateralAsset];
        require(uBorrowerColl.useAsCollateral, "Pool: not collat");

        ReserveData storage rDebt = reserves[debtAsset];
        ReserveData storage rColl = reserves[collateralAsset];

        uint256 borrowerDebt = (uBorrowerDebt.scaledBorrow * rDebt.borrowIndex) / WadMath.RAY;
        require(borrowerDebt > 0, "Pool: no debt");

        // max repayable = closeFactor × debt
        uint256 maxRepay = (borrowerDebt * CLOSE_FACTOR_BPS) / BPS;
        uint256 actualRepay = debtToCover > maxRepay ? maxRepay : debtToCover;

        // ----- collateral to seize -----
        // debt value (USD WAD) = actualRepay * priceDebt / 10^decimalsDebt
        // collateral to seize (native units) =
        //   debtValue * (1 + bonus) * 10^decimalsColl / priceColl
        uint256 priceDebt = oracle.getAssetPrice(debtAsset);
        uint256 priceColl = oracle.getAssetPrice(collateralAsset);
        uint8 decDebt = IERC20(debtAsset).decimals();
        uint8 decColl = IERC20(collateralAsset).decimals();

        // value in USD-WAD of debt being repaid
        uint256 debtValueWad = (actualRepay * priceDebt) / (10 ** decDebt);
        // apply liquidation bonus to liquidator
        uint256 bonusedValueWad = debtValueWad + (debtValueWad * rColl.liquidationBonusBps) / BPS;
        // collateral amount (native units) = bonusedValue / priceColl, denominated in collateral decimals
        uint256 collateralToSeize = (bonusedValueWad * (10 ** decColl)) / priceColl;

        // cap at borrower's actual collateral
        uint256 borrowerCollActual = (uBorrowerColl.scaledSupply * rColl.liquidityIndex) / WadMath.RAY;
        if (collateralToSeize > borrowerCollActual) {
            collateralToSeize = borrowerCollActual;
            // recompute repay so liquidator doesn't overpay (rare, only when collateral is exhausted)
            uint256 newDebtValueWad = (collateralToSeize * priceColl) / (10 ** decColl);
            newDebtValueWad = (newDebtValueWad * BPS) / (BPS + rColl.liquidationBonusBps);
            actualRepay = (newDebtValueWad * (10 ** decDebt)) / priceDebt;
        }

        // ----- effects -----
        uint256 scaledDebtBurn = (actualRepay * WadMath.RAY) / rDebt.borrowIndex;
        if (scaledDebtBurn > uBorrowerDebt.scaledBorrow) scaledDebtBurn = uBorrowerDebt.scaledBorrow;
        uBorrowerDebt.scaledBorrow -= scaledDebtBurn;
        rDebt.totalScaledBorrow -= scaledDebtBurn;

        uint256 scaledCollBurn = (collateralToSeize * WadMath.RAY) / rColl.liquidityIndex;
        if (scaledCollBurn > uBorrowerColl.scaledSupply) scaledCollBurn = uBorrowerColl.scaledSupply;
        uBorrowerColl.scaledSupply -= scaledCollBurn;
        rColl.totalScaledSupply -= scaledCollBurn;

        // ----- interactions -----
        _safeTransferFrom(debtAsset, msg.sender, address(this), actualRepay);
        _safeTransfer(collateralAsset, msg.sender, collateralToSeize);

        emit Liquidation(msg.sender, borrower, debtAsset, collateralAsset, actualRepay, collateralToSeize);
    }

    // ===================================================================
    // Flash loans
    // ===================================================================
    /// @notice Borrow `amount` of `asset` for the duration of a single transaction.
    /// @dev    Receiver must implement IFlashLoanReceiver. The pool transfers
    ///         `amount` to the receiver, invokes its callback, and then pulls
    ///         (amount + fee) back via `transferFrom`. The receiver must have
    ///         approved the pool to spend (amount + fee) before the callback returns.
    ///
    ///         The flash loan reentrancy lock is the same as the regular lock —
    ///         flash loans cannot recursively call other pool functions. This is
    ///         intentional and prevents the most common flash-loan attack
    ///         pattern (re-entering with price-oracle manipulation mid-call).
    ///
    /// @param receiver    Contract implementing IFlashLoanReceiver
    /// @param asset       Token to borrow
    /// @param amount      Amount to borrow (native decimals)
    /// @param data        Arbitrary data passed through to the receiver
    function flashLoan(
        address receiver,
        address asset,
        uint256 amount,
        bytes calldata data
    ) external nonReentrant whenNotPaused {
        require(reserveExists[asset] && reserves[asset].active, "Pool: inactive");
        require(receiver != address(0) && amount > 0, "Pool: bad args");
        require(_availableLiquidity(asset) >= amount, "Pool: liquidity");

        _accrueInterest(asset);

        uint256 fee = (amount * flashLoanFeeBps) / BPS;
        uint256 balanceBefore = IERC20(asset).balanceOf(address(this));

        // Send tokens to receiver
        _safeTransfer(asset, receiver, amount);

        // Invoke callback
        IFlashLoanReceiver(receiver).onFlashLoan(msg.sender, asset, amount, fee, data);

        // Pull back principal + fee
        _safeTransferFrom(asset, receiver, address(this), amount + fee);

        // Verify pool was made whole
        uint256 balanceAfter = IERC20(asset).balanceOf(address(this));
        require(balanceAfter >= balanceBefore + fee, "Pool: not repaid");

        // Fee accrues to treasury as scaled supply
        if (fee > 0) {
            ReserveData storage r = reserves[asset];
            r.accruedToTreasuryScaled += (fee * WadMath.RAY) / r.liquidityIndex;
        }

        emit FlashLoan(receiver, msg.sender, asset, amount, fee);
    }

    // ===================================================================
    // Interest accrual
    // ===================================================================
    function _accrueInterest(address asset) internal {
        ReserveData storage r = reserves[asset];
        uint256 elapsed = block.timestamp - r.lastUpdateTimestamp;
        if (elapsed == 0) return;

        uint256 util = _utilizationWad(asset);
        IInterestRateModel irm = IInterestRateModel(r.interestRateModel);
        uint256 borrowRateRay = irm.getBorrowRatePerSecond(util);
        uint256 supplyRateRay = irm.getSupplyRatePerSecond(util, uint256(r.reserveFactorBps) * WadMath.WAD / BPS);

        uint256 borrowGrowth = WadMath.compoundFactor(borrowRateRay, elapsed);
        uint256 supplyGrowth = WadMath.compoundFactor(supplyRateRay, elapsed);

        uint256 oldBorrowIdx = r.borrowIndex;
        uint256 oldLiqIdx = r.liquidityIndex;
        uint256 newBorrowIdx = (oldBorrowIdx * borrowGrowth) / WadMath.RAY;
        uint256 newLiqIdx = (oldLiqIdx * supplyGrowth) / WadMath.RAY;

        // Treasury accrual: the asymmetry between borrower interest paid
        // and supplier interest received accrues as a scaled claim.
        //   actualBorrowDelta = totalScaledBorrow * (newBI - oldBI) / RAY
        //   actualSupplyDelta = totalScaledSupply * (newLI - oldLI) / RAY
        //   treasuryDelta     = actualBorrowDelta - actualSupplyDelta
        if (r.reserveFactorBps != 0 && r.totalScaledBorrow != 0) {
            uint256 actualBorrowDelta =
                (r.totalScaledBorrow * (newBorrowIdx - oldBorrowIdx)) / WadMath.RAY;
            uint256 actualSupplyDelta =
                (r.totalScaledSupply * (newLiqIdx - oldLiqIdx)) / WadMath.RAY;
            if (actualBorrowDelta > actualSupplyDelta) {
                uint256 treasuryDelta = actualBorrowDelta - actualSupplyDelta;
                r.accruedToTreasuryScaled += (treasuryDelta * WadMath.RAY) / newLiqIdx;
            }
        }

        r.borrowIndex = uint128(newBorrowIdx);
        r.liquidityIndex = uint128(newLiqIdx);
        r.lastUpdateTimestamp = uint40(block.timestamp);
    }

    // ===================================================================
    // Views
    // ===================================================================
    /// @notice Returns aggregate account data across all reserves.
    /// @return totalCollateralWad   sum of (collateral × price) for all enabled collaterals, USD WAD
    /// @return totalDebtWad         sum of (debt × price) for all debts, USD WAD
    /// @return availableBorrowsWad  remaining borrow capacity at user's average LTV
    /// @return currentLiquidationThresholdBps   weighted-avg liquidation threshold (bps)
    /// @return ltvBps               weighted-avg LTV (bps)
    /// @return healthFactor         (totalCollateral × liqThreshold) / totalDebt, WAD; max uint if no debt
    function getUserAccountData(address user)
        external
        view
        returns (
            uint256 totalCollateralWad,
            uint256 totalDebtWad,
            uint256 availableBorrowsWad,
            uint256 currentLiquidationThresholdBps,
            uint256 ltvBps,
            uint256 healthFactor
        )
    {
        return _userAccountData(user);
    }

    function getReserveData(address asset) external view returns (
        uint256 liquidityIndex,
        uint256 borrowIndex,
        uint256 totalSupply,
        uint256 totalBorrow,
        uint256 utilizationWad,
        uint256 borrowRatePerSecondRay,
        uint256 supplyRatePerSecondRay
    ) {
        ReserveData storage r = reserves[asset];
        // Compute "live" indices including pending interest since last update
        (uint256 liqIdx, uint256 borIdx) = _pendingIndices(asset);
        liquidityIndex = liqIdx;
        borrowIndex = borIdx;
        totalSupply = (r.totalScaledSupply * liqIdx) / WadMath.RAY;
        totalBorrow = (r.totalScaledBorrow * borIdx) / WadMath.RAY;
        utilizationWad = totalSupply == 0 ? 0 : (totalBorrow * WadMath.WAD) / totalSupply;

        IInterestRateModel irm = IInterestRateModel(r.interestRateModel);
        borrowRatePerSecondRay = irm.getBorrowRatePerSecond(utilizationWad);
        supplyRatePerSecondRay = irm.getSupplyRatePerSecond(
            utilizationWad,
            uint256(r.reserveFactorBps) * WadMath.WAD / BPS
        );
    }

    function getUserReserveData(address user, address asset) external view returns (
        uint256 supplied,
        uint256 borrowed,
        bool useAsCollateral
    ) {
        UserData storage u = users[user][asset];
        (uint256 liqIdx, uint256 borIdx) = _pendingIndices(asset);
        supplied = (u.scaledSupply * liqIdx) / WadMath.RAY;
        borrowed = (u.scaledBorrow * borIdx) / WadMath.RAY;
        useAsCollateral = u.useAsCollateral;
    }

    function getReservesList() external view returns (address[] memory) {
        return reservesList;
    }

    // ===================================================================
    // Internals
    // ===================================================================
    function _userAccountData(address user) internal view returns (
        uint256 totalCollateralWad,
        uint256 totalDebtWad,
        uint256 availableBorrowsWad,
        uint256 currentLiquidationThresholdBps,
        uint256 ltvBps,
        uint256 hf
    ) {
        uint256 weightedLiqThreshold; // numerator for weighted avg
        uint256 weightedLtv;

        uint256 len = reservesList.length;
        for (uint256 i = 0; i < len; i++) {
            address asset = reservesList[i];
            ReserveData storage r = reserves[asset];
            UserData storage u = users[user][asset];
            (uint256 liqIdx, uint256 borIdx) = _pendingIndices(asset);

            uint256 price = oracle.getAssetPrice(asset);
            uint8 dec = IERC20(asset).decimals();

            if (u.scaledSupply > 0 && u.useAsCollateral && r.usableAsCollateral) {
                uint256 amount = (u.scaledSupply * liqIdx) / WadMath.RAY;
                uint256 valueWad = (amount * price) / (10 ** dec);
                totalCollateralWad += valueWad;
                weightedLiqThreshold += valueWad * r.liquidationThresholdBps;
                weightedLtv += valueWad * r.ltvBps;
            }

            if (u.scaledBorrow > 0) {
                uint256 amount = (u.scaledBorrow * borIdx) / WadMath.RAY;
                uint256 valueWad = (amount * price) / (10 ** dec);
                totalDebtWad += valueWad;
            }
        }

        if (totalCollateralWad > 0) {
            currentLiquidationThresholdBps = weightedLiqThreshold / totalCollateralWad;
            ltvBps = weightedLtv / totalCollateralWad;
            availableBorrowsWad = (totalCollateralWad * ltvBps) / BPS;
            availableBorrowsWad = availableBorrowsWad > totalDebtWad
                ? availableBorrowsWad - totalDebtWad
                : 0;
        }

        if (totalDebtWad == 0) {
            hf = type(uint256).max;
        } else {
            // HF = (collateral × liqThreshold) / debt
            hf = (totalCollateralWad * currentLiquidationThresholdBps * WadMath.WAD)
                / (BPS * totalDebtWad);
        }
    }

    function _healthFactor(address user) internal view returns (uint256 hf) {
        (, , , , , hf) = _userAccountData(user);
    }

    function _userHasDebt(address user) internal view returns (bool) {
        uint256 len = reservesList.length;
        for (uint256 i = 0; i < len; i++) {
            if (users[user][reservesList[i]].scaledBorrow > 0) return true;
        }
        return false;
    }

    function _utilizationWad(address asset) internal view returns (uint256) {
        ReserveData storage r = reserves[asset];
        uint256 totalSupply = (r.totalScaledSupply * r.liquidityIndex) / WadMath.RAY;
        if (totalSupply == 0) return 0;
        uint256 totalBorrow = (r.totalScaledBorrow * r.borrowIndex) / WadMath.RAY;
        return (totalBorrow * WadMath.WAD) / totalSupply;
    }

    function _availableLiquidity(address asset) internal view returns (uint256) {
        ReserveData storage r = reserves[asset];
        // Total claims on pool tokens = suppliers + treasury; both are denominated
        // in the same scaled units against the liquidity index.
        uint256 supplyClaim =
            ((r.totalScaledSupply + r.accruedToTreasuryScaled) * r.liquidityIndex) / WadMath.RAY;
        uint256 borrowObligation = (r.totalScaledBorrow * r.borrowIndex) / WadMath.RAY;
        return supplyClaim > borrowObligation ? supplyClaim - borrowObligation : 0;
    }

    function _pendingIndices(address asset) internal view returns (uint256 liqIdx, uint256 borIdx) {
        ReserveData storage r = reserves[asset];
        liqIdx = r.liquidityIndex;
        borIdx = r.borrowIndex;
        uint256 elapsed = block.timestamp - r.lastUpdateTimestamp;
        if (elapsed == 0) return (liqIdx, borIdx);

        uint256 util = _utilizationWad(asset);
        IInterestRateModel irm = IInterestRateModel(r.interestRateModel);
        uint256 borrowRateRay = irm.getBorrowRatePerSecond(util);
        uint256 supplyRateRay = irm.getSupplyRatePerSecond(
            util,
            uint256(r.reserveFactorBps) * WadMath.WAD / BPS
        );

        liqIdx = (liqIdx * WadMath.compoundFactor(supplyRateRay, elapsed)) / WadMath.RAY;
        borIdx = (borIdx * WadMath.compoundFactor(borrowRateRay, elapsed)) / WadMath.RAY;
    }

    // ---- transfer helpers ---------------------------------------------
    function _safeTransfer(address token, address to, uint256 amount) internal {
        (bool ok, bytes memory data) = token.call(
            abi.encodeWithSelector(IERC20.transfer.selector, to, amount)
        );
        require(ok && (data.length == 0 || abi.decode(data, (bool))), "Pool: xfer failed");
    }

    function _safeTransferFrom(address token, address from, address to, uint256 amount) internal {
        (bool ok, bytes memory data) = token.call(
            abi.encodeWithSelector(IERC20.transferFrom.selector, from, to, amount)
        );
        require(ok && (data.length == 0 || abi.decode(data, (bool))), "Pool: xferFrom failed");
    }
}
