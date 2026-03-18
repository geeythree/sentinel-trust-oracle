#!/bin/bash
# Sentinel — One-shot setup and evaluation script
# Run this after: (1) funding wallets, (2) setting VENICE_API_KEY in .env
set -e

cd "$(dirname "$0")"
source env/bin/activate

echo "============================================"
echo "  SENTINEL — SETUP & EVALUATION"
echo "============================================"

# Check prerequisites
echo ""
echo "[1/7] Checking prerequisites..."

python3 -c "
from config import create_config
from eth_account import Account
from web3 import Web3

c = create_config()
w3 = Web3(Web3.HTTPProvider(c.rpc_url))

op = Account.from_key(c.OPERATOR_PRIVATE_KEY)
ev = Account.from_key(c.EVALUATOR_PRIVATE_KEY)
op_bal = float(w3.from_wei(w3.eth.get_balance(op.address), 'ether'))
ev_bal = float(w3.from_wei(w3.eth.get_balance(ev.address), 'ether'))

print(f'  Operator:  {op.address}  ({op_bal:.4f} ETH)')
print(f'  Evaluator: {ev.address}  ({ev_bal:.4f} ETH)')
print(f'  Venice:    {\"SET\" if c.VENICE_API_KEY else \"MISSING\"}')
print(f'  Network:   {\"Sepolia\" if c.USE_TESTNET else \"Mainnet\"}')

errors = []
if op_bal < 0.001:
    errors.append('OPERATOR wallet has insufficient funds (need >0.001 ETH)')
if ev_bal < 0.001:
    errors.append('EVALUATOR wallet has insufficient funds (need >0.001 ETH)')
if not c.VENICE_API_KEY:
    errors.append('VENICE_API_KEY is not set in .env')
if errors:
    print()
    for e in errors:
        print(f'  ERROR: {e}')
    exit(1)
print('  All checks passed!')
"

# Step 2: Register EAS schema
echo ""
echo "[2/7] Registering EAS trust verdict schema..."

if grep -q "^EAS_SCHEMA_UID=$" .env 2>/dev/null || ! grep -q "EAS_SCHEMA_UID" .env; then
    SCHEMA_UID=$(python3 -c "
import config as config_module
config_module.config = config_module.create_config()
from blockchain import BlockchainClient
from logger import AgentLogger
from config import config
lg = AgentLogger(config.AGENT_LOG_PATH, budget=15)
bc = BlockchainClient(lg)
uid = bc.register_eas_schema()
print(uid)
")
    echo "  Schema UID: $SCHEMA_UID"
    # Update .env with schema UID
    if [[ "$OSTYPE" == "darwin"* ]]; then
        sed -i '' "s/^EAS_SCHEMA_UID=.*/EAS_SCHEMA_UID=$SCHEMA_UID/" .env
    else
        sed -i "s/^EAS_SCHEMA_UID=.*/EAS_SCHEMA_UID=$SCHEMA_UID/" .env
    fi
    echo "  Updated .env with EAS_SCHEMA_UID"
else
    echo "  EAS_SCHEMA_UID already set, skipping"
fi

# Step 3: Host agent.json and register identity
echo ""
echo "[3/7] Registering Sentinel's ERC-8004 identity..."

# Use data: URI for agent.json (no hosting needed)
AGENT_JSON_B64=$(python3 -c "
import base64, json
with open('agent.json') as f:
    data = f.read()
encoded = base64.b64encode(data.encode()).decode()
print(f'data:application/json;base64,{encoded}')
")

python3 main.py register --agent-uri "$AGENT_JSON_B64" --testnet 2>&1 || echo "  (May fail if already registered)"

# Step 4: Discover and evaluate agents
echo ""
echo "[4/7] Discovering and evaluating agents on Base Sepolia..."
python3 main.py discover --testnet --max-agents 3

# Step 5: Manual evaluation of a specific agent
echo ""
echo "[5/7] Manual evaluation of Agent #1817..."
python3 main.py manual --agent-id 1817 --testnet

# Step 6: Show results
echo ""
echo "[6/7] Results saved to dashboard/results.json"
if [ -f dashboard/results.json ]; then
    python3 -c "
import json
with open('dashboard/results.json') as f:
    data = json.load(f)
print(f'  {len(data)} evaluations in dashboard')
for d in data:
    print(f'  Agent #{d[\"agent_id\"]}: score={d[\"composite_score\"]} conf={d[\"confidence\"]} state={d[\"state\"]}')
"
fi

# Step 7: Show agent_log.json stats
echo ""
echo "[7/7] Agent log summary"
if [ -f agent_log.json ]; then
    python3 -c "
import json
entries = []
with open('agent_log.json') as f:
    for line in f:
        line = line.strip()
        if line:
            entries.append(json.loads(line))
print(f'  {len(entries)} log entries')
actions = {}
for e in entries:
    at = e.get('action_type', 'unknown')
    actions[at] = actions.get(at, 0) + 1
for k, v in sorted(actions.items()):
    print(f'    {k}: {v}')
"
fi

echo ""
echo "============================================"
echo "  SETUP COMPLETE!"
echo "============================================"
echo ""
echo "Next steps:"
echo "  1. Push dashboard: git add -A && git commit -m 'Add evaluation results' && git push"
echo "  2. Test MCP: python3 dummy_client_agent.py --agent-id 1817"
echo "  3. Mainnet: python3 main.py discover --mainnet --max-agents 3"
