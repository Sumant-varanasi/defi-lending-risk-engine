# Frontend — Lending Terminal

Single-file vanilla web app. No build step, no npm. Open `index.html` in
Chrome (or Brave/Firefox/Edge — anything with MetaMask) and it works.

## First-time setup (5 minutes)

### 1. Get MetaMask
Install the browser extension if you don't have it: https://metamask.io

### 2. Add the Anvil network to MetaMask
- Open MetaMask → click the network dropdown (top-left) → "Add a custom network"
- Network name: `Anvil Local`
- New RPC URL: `http://127.0.0.1:8545`
- Chain ID: `31337`
- Currency symbol: `ETH`
- Save

### 3. Import a test account
When you run `anvil`, it prints 10 prefunded accounts with their private keys.
Copy the **first private key** (long hex string).
- In MetaMask → click account icon (top-right) → "Add account or hardware wallet" → "Import account"
- Paste the private key → Import
- You'll see an account with 10,000 ETH

### 4. Deploy the contracts
From a separate terminal, with `anvil` already running:
```bash
cd contracts
forge script script/Deploy.s.sol \
  --rpc-url http://127.0.0.1:8545 \
  --broadcast \
  --private-key 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80
```
The script prints addresses at the end (look for the `---DEPLOYMENT---` block).

### 5. Paste addresses into `index.html`
Open `index.html` in any text editor and find the `CONFIG = {` block near
the bottom. Fill in:

```js
const CONFIG = {
  chainId: 31337,
  rpcUrl: "http://127.0.0.1:8545",
  pool:    "0xPASTE_POOL_ADDRESS",
  oracle:  "0xPASTE_ORACLE_ADDRESS",
  assets: [
    { symbol: "WETH", address: "0xPASTE_WETH", decimals: 18, isCollateral: true,  isBorrowable: false },
    { symbol: "USDC", address: "0xPASTE_USDC", decimals: 6,  isCollateral: true,  isBorrowable: true  },
    { symbol: "WBTC", address: "0xPASTE_WBTC", decimals: 8,  isCollateral: true,  isBorrowable: false },
  ],
};
```

Save the file.

### 6. Open the frontend
Double-click `index.html` (or right-click → Open with → Chrome).
Click **Connect Wallet** in the top-right. Approve MetaMask. Done.

## What you can do in the UI

- **Pool Overview cards** — supply TVL, borrowed, utilization, LTV/threshold,
  reserve factor per asset.
- **Your Position panel** — total collateral, debt, available borrows,
  health factor with color-coded bar (green > 2.0, lime > 1.3, orange > 1.0,
  red < 1.0). Per-asset breakdown of what you've supplied vs borrowed.
- **Actions panel**:
  - **Approve** — required before deposit/repay (ERC-20 standard)
  - **Deposit** — supply collateral, start earning yield
  - **Withdraw** — pull supplied tokens back
  - **Borrow** — take a loan against your collateral
  - **Repay** — pay down debt + accrued interest

## Demo flow (use this for your presentation)

1. **Connect wallet** — wallet drops in showing 10k ETH
2. **Mint test tokens** — the deploy script sent you 1000 WETH, 10M USDC, 100 WBTC
3. **Approve WETH** for the pool
4. **Deposit 10 WETH** — see TVL update, your position appear
5. **Borrow 5,000 USDC** — see HF appear at ~3.2x, debt panel populate
6. **Crash ETH price**:
   ```bash
   cast send <ORACLE_ADDRESS> "setPrice(address,uint256)" <WETH_ADDRESS> 1000000000000000000000 \
     --private-key 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80 \
     --rpc-url http://127.0.0.1:8545
   ```
   (1000e18 = $1000 per ETH, down from $2000)
7. **Refresh the page** — your HF drops to ~1.6x, bar shifts orange
8. **Crash further** to $500/ETH — HF drops below 1.0, bar turns red
9. **Run the liquidator bot** (from project root):
   ```bash
   PYTHONPATH=. python -m python.liquidator.bot
   ```
   It detects your unhealthy position and liquidates ~50% of your debt,
   seizing collateral at a 5% bonus.
10. **Refresh** — your debt and collateral shrink, HF recovers above 1.

That's the whole protocol in 10 steps.

## Troubleshooting

- **"MetaMask not found"** — extension not installed, or you opened the file
  in a browser that doesn't have it.
- **"Wrong network"** — switch MetaMask to your Anvil Local network.
- **"Frontend not configured"** — you didn't fill in the CONFIG block. Step 5.
- **`Transaction failed: HF below 1`** — you tried to borrow/withdraw more
  than your collateral allows. Reduce the amount.
- **Numbers don't refresh** — the page auto-refreshes every 10s, or you can
  reload. If the chain restarted (anvil dies), redeploy and update addresses.
- **MetaMask says "Internal JSON-RPC error"** — usually means a `require`
  reverted in the contract. Check the browser console for the underlying
  message.

## What this frontend is NOT

It's deliberately minimal. Things you'd add for a real product:
- Account abstraction for gas-free UX
- Transaction history view
- Detailed APY/APR display (would need to compute from indices)
- Multi-step flows ("supply + borrow in one tx" via flashloan)
- Wallet onboarding (RainbowKit / Web3Modal)
- Dark/light theme toggle
- Mobile responsiveness beyond basic Tailwind

For a semester project demo, it's plenty.
