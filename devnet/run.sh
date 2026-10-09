#!/bin/sh
# Deploys BRLC behind a proxy on the private Anvil network and sends real transactions (PHASE4 T6).
# Keys: Anvil's public test accounts, which exist only on this local test network.
set -e
RPC=http://127.0.0.1:8545
OWNER_KEY=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80
ALICE_KEY=0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d
BOB_KEY=0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a
OWNER=0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266
ALICE=0x70997970C51812dc3A010C7d01b50e0d17dc79C8
BOB=0x3C44CdDdB6a900fa2b585d8C4E1a7c9Ee5F0F9C3
cd /w
IMPL=$(forge create contracts/BRLCToken.sol:BRLCToken --rpc-url $RPC --private-key $OWNER_KEY --broadcast | awk "/Deployed to:/ {print \$3}")
INIT=$(cast calldata "initialize(string,string)" "BRL Coin" "BRLC")
TOKEN=$(forge create contracts/devnet/Proxy.sol:DevnetProxy --rpc-url $RPC --private-key $OWNER_KEY --broadcast --constructor-args $IMPL $INIT | awk "/Deployed to:/ {print \$3}")
echo "IMPL=$IMPL"; echo "TOKEN=$TOKEN"
send() { label=$1; shift; h=$(cast send "$@" --rpc-url $RPC --json 2>/dev/null | sed -n "s/.*\"transactionHash\":\"\(0x[0-9a-f]*\)\".*/\1/p"); echo "$label=$h"; }
send SETUP_MAIN_MINTER $TOKEN "updateMainMinter(address)" $OWNER --private-key $OWNER_KEY
send SETUP_MINTER $TOKEN "configureMinter(address,uint256)" $OWNER 1000000000000 --private-key $OWNER_KEY
send MINT $TOKEN "mint(address,uint256)" $ALICE 1000000000 --private-key $OWNER_KEY
send TRANSFER $TOKEN "transfer(address,uint256)" $BOB 100000000 --private-key $ALICE_KEY
send APPROVE $TOKEN "approve(address,uint256)" $BOB 50000000 --private-key $ALICE_KEY
send FAIL_ALLOWANCE $TOKEN "transferFrom(address,address,uint256)" $ALICE $BOB 80000000 --private-key $BOB_KEY --gas-limit 200000
send SET_PAUSER $TOKEN "setPauser(address)" $OWNER --private-key $OWNER_KEY
send PAUSE $TOKEN "pause()" --private-key $OWNER_KEY
send FAIL_PAUSED $TOKEN "transfer(address,uint256)" $BOB 1000000 --private-key $ALICE_KEY --gas-limit 200000
send UNPAUSE $TOKEN "unpause()" --private-key $OWNER_KEY
send FAIL_ACCESS $TOKEN "setPauser(address)" $BOB --private-key $BOB_KEY --gas-limit 200000
send FAIL_OUT_OF_GAS $TOKEN "transfer(address,uint256)" $BOB 1000000 --private-key $ALICE_KEY --gas-limit 30000
send TRANSFER_AFTER $TOKEN "transfer(address,uint256)" $BOB 1000000 --private-key $ALICE_KEY
