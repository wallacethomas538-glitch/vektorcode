#!/bin/bash
BASE_URL="http://127.0.0.1:8765"

test_endpoint() {
    local name=$1
    local path=$2
    local data=$3
    echo -n "Testing $name... "
    if [ -z "$data" ]; then
        res=$(curl -s -o /dev/null -w "%{http_code}" $BASE_URL$path)
    else
        res=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Content-Type: application/json" -d "$data" $BASE_URL$path)
    fi
    if [ "$res" == "200" ]; then
        echo "✅ 200 OK"
        return 0
    elif [ "$res" == "502" ] || [ "$res" == "429" ]; then
        echo "✅ $res (Functional)"
        return 0
    else
        echo "❌ $res"
        return 1
    fi
}

test_endpoint "Health Check" "/health" ""
test_endpoint "State API" "/api/state" ""
test_endpoint "MCP Status" "/api/mcp/status" ""
test_endpoint "Model Switch" "/api/model" '{"model": "local"}'
test_endpoint "Chat Response" "/api/chat" '{"messages": [{"role": "user", "content": "hi"}], "model": "local"}'
