#!/usr/bin/env python3
"""
Phone Agent HTTP Server - 将 Phone Agent 作为 HTTP 服务运行

使用方法：
    python server.py --port 8080

API 端点：
    POST /run  - 执行任务
    GET  /status - 查看状态
    POST /reset - 重置 Agent
"""

import argparse
import json
from flask import Flask, request, jsonify
from phone_agent import PhoneAgent
from phone_agent.model import ModelConfig
from phone_agent.agent import AgentConfig

app = Flask(__name__)

# 全局 Agent 实例
agent = None
agent_initialized = False


def initialize_agent(base_url: str, model: str, api_key: str):
    """初始化 Agent"""
    global agent, agent_initialized
    
    model_config = ModelConfig(
        base_url=base_url,
        model_name=model,
        api_key=api_key,
    )
    
    agent_config = AgentConfig(
        max_steps=100,
        verbose=True,
    )
    
    agent = PhoneAgent(
        model_config=model_config,
        agent_config=agent_config,
    )
    
    agent_initialized = True
    print("✅ Agent initialized successfully")


@app.route('/run', methods=['POST'])
def run_task():
    """执行任务"""
    if not agent_initialized:
        return jsonify({
            'success': False,
            'error': 'Agent not initialized'
        }), 500
    
    data = request.get_json()
    task = data.get('task', '')
    print(data)
    
    if not task:
        return jsonify({
            'success': False,
            'error': 'Task is required'
        }), 400
    
    try:
        print(f"\n📱 Running task: {task}")
        result = agent.run(task)
        
        return jsonify({
            'success': True,
            'task': task,
            'result': result
        })
    
    except Exception as e:
        print(f"❌ Error: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/status', methods=['GET'])
def get_status():
    """获取状态"""
    return jsonify({
        'status': 'running' if agent_initialized else 'not_initialized',
        'agent_initialized': agent_initialized,
        'step_count': agent._step_count if agent else 0
    })


@app.route('/reset', methods=['POST'])
def reset_agent():
    """重置 Agent"""
    if agent:
        agent.reset()
        return jsonify({'success': True, 'message': 'Agent reset'})
    return jsonify({'success': False, 'error': 'Agent not initialized'}), 500


@app.route('/health', methods=['GET'])
def health_check():
    """健康检查"""
    return jsonify({'status': 'healthy'})


def main():
    parser = argparse.ArgumentParser(description='Phone Agent HTTP Server')
    parser.add_argument('--host', default='127.0.0.1', help='Host to bind to')
    parser.add_argument('--port', type=int, default=8080, help='Port to listen on')
    parser.add_argument('--base-url', default='http://open.bigmodel.cn/api/paas/v4', 
                        help='Model API base URL')
    parser.add_argument('--model', default='autoglm-phone', help='Model name')
    parser.add_argument('--apikey', default='a76cba048d6e4660bd898e8c01745633.shbyEvx1hWf3CkYF', help='API key')
    # parser.add_argument(
    #     "--connect",
    #     "-c",
    #     type=str,
    #     metavar="127.0.0.1:21503",
    #     help="Connect to remote device (e.g., 192.168.1.100:5555)",
    # )
    args = parser.parse_args()
    
    # Initialize agent
    print("🔧 Initializing Phone Agent...")
    initialize_agent(
        base_url=args.base_url,
        model=args.model,
        api_key=args.apikey
    )
    
    # Start server
    print(f"🚀 Starting server on {args.host}:{args.port}")
    print(f"📡 API endpoints:")
    print(f"   POST http://{args.host}:{args.port}/run")
    print(f"   GET  http://{args.host}:{args.port}/status")
    print(f"   POST http://{args.host}:{args.port}/reset")
    print(f"   GET  http://{args.host}:{args.port}/health")
    print("\nPress Ctrl+C to stop")
    
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == '__main__':
    main()
