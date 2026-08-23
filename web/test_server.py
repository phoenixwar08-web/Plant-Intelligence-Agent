from flask import Flask
app = Flask(__name__)

@app.route('/')
def index():
    return "✅ 服务器正常连通！网页访问成功！"

if __name__ == '__main__':
    print("测试服务启动中，端口6000")
    app.run(host='0.0.0.0', port=6000, debug=False, use_reloader=False)
