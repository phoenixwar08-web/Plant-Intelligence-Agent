#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import json
from pathlib import Path
from datetime import datetime


BASE_DIR = Path(__file__).resolve().parent.parent

INPUT_DIR = BASE_DIR / "outputs"



def load_status(device_code):

    path = INPUT_DIR / f"{device_code}_status.json"

    if not path.exists():
        raise FileNotFoundError(path)

    return json.loads(
        path.read_text(
            encoding="utf-8"
        )
    )



def analyze_sensor(sensor):

    humidity = sensor.get(
        "soil_humidity"
    )


    if humidity is None:

        return {
            "risk":"unknown",
            "message":"缺少土壤湿度"
        }


    if humidity < 20:

        return {
            "risk":"dry",
            "message":
            f"土壤湿度{humidity}%，偏干"
        }


    elif humidity < 35:

        return {
            "risk":"attention",
            "message":
            f"土壤湿度{humidity}%，需要关注"
        }


    else:

        return {
            "risk":"normal",
            "message":
            f"土壤湿度{humidity}%，正常"
        }




def analyze_visual(visual):

    if not visual:

        return {
            "risk":"unknown",
            "message":"无视觉数据"
        }


    yellow = visual.get(
        "yellow_leaf_ratio"
    )


    disease = visual.get(
        "disease_suspected",
        False
    )


    issues=[]


    risk="normal"



    if yellow is not None:

        if yellow > 0.3:

            risk="warning"

            issues.append(
                f"黄叶比例高:{yellow}"
            )


        elif yellow > 0.15:

            risk="attention"

            issues.append(
                f"黄叶比例升高:{yellow}"
            )


    if disease:

        risk="warning"

        issues.append(
            "疑似病害"
        )


    if not issues:

        issues.append(
            "视觉正常"
        )


    return {

        "risk":risk,

        "message":
        "、".join(issues),

        "plant_health":
        visual.get(
            "plant_health"
        ),

        "yellow_leaf_ratio":
        yellow,

        "flower_count":
        visual.get(
            "flower_count"
        ),

        "fruit_count":
        visual.get(
            "fruit_count"
        )

    }



def analyze_fusion(
    sensor,
    visual
):

    sources=[]


    if sensor["risk"]=="dry":

        sources.append(
            "soil_dry"
        )


    elif sensor["risk"]=="attention":

        sources.append(
            "soil_attention"
        )


    if visual["risk"]=="attention":

        sources.append(
            "visual_attention"
        )


    if visual["risk"]=="warning":

        sources.append(
            "visual_warning"
        )


    if (
        "soil_dry" in sources
        and
        "visual_warning"
        not in sources
    ):

        level="dry"


    elif "visual_warning" in sources:

        level="warning"


    elif sources:

        level="attention"


    else:

        level="normal"



    return {

        "risk":level,

        "risk_sources":
        sources,

        "message":
        "、".join(sources)
        if sources
        else
        "正常"

    }
    
def analyze_control(control):

    return {

        "current_strategy":
            control.get(
                "decision"
            ),

        "reason":
            control.get(
                "decision_reason"
            )
    }



def analyze_history(history):

    events = history.get(
        "recent_irrigation_events",
        []
    )


    if not events:

        return {

            "recent_irrigation_count":0,

            "message":
            "近期无灌溉记录"

        }


    total = sum(

        float(
            e.get(
                "water_sec",
                0
            )
        )

        for e in events

    )


    return {

        "recent_irrigation_count":
            len(events),

        "recent_water_sec":
            round(
                total,
                2
            ),

        "message":
            f"最近{len(events)}次灌溉，共{total:.1f}秒"

    }


def analyze_human_events(human_events):
    """Create explanatory context only; this function never participates in control decisions."""
    latest = (human_events or {}).get("last_external_watering")
    if (
        not isinstance(latest, dict)
        or latest.get("confirmation_status", "confirmed") != "confirmed"
        or latest.get("trust_status") not in {"attested", "legacy_verified"}
        or not latest.get("occurred_at")
    ):
        return {"message": "近期无人工浇水记录", "last_external_watering": None}

    details = [f"最近人工浇水记录：{latest['occurred_at']}"]
    duration = latest.get("duration_sec")
    volume = latest.get("volume_ml")
    if isinstance(duration, (int, float)):
        details.append(f"时长 {duration:g} 秒")
    if isinstance(volume, (int, float)):
        details.append(f"水量 {volume:g} ml")
    if latest.get("note"):
        details.append(f"备注：{latest['note']}")
    return {"message": "；".join(details), "last_external_watering": latest}


def analyze_manual_check(human_events):
    """Return an audit fact only; manual feedback cannot clear safety or change control."""
    latest = (human_events or {}).get("last_manual_check")
    if (
        not isinstance(latest, dict)
        or latest.get("confirmation_status") != "confirmed"
        or latest.get("trust_status") not in {"attested", "legacy_verified"}
        or not latest.get("occurred_at")
        or not latest.get("check_code")
        or latest.get("result") not in {"no_issue", "issue_found", "not_completed"}
    ):
        return {"message": "近期无人工检查记录", "last_manual_check": None}

    labels = {
        "no_issue": "未发现异常",
        "issue_found": "发现异常",
        "not_completed": "无法完成检查",
    }
    details = [
        f"最近人工检查记录：{latest['occurred_at']}",
        f"检查项 {latest['check_code']}",
        f"结果 {labels[latest['result']]}",
    ]
    if latest.get("note"):
        details.append(f"备注：{latest['note']}")
    return {"message": "；".join(details), "last_manual_check": latest}



def calculate_water_time(humidity):

    if humidity is None:
        return 3


    if humidity < 10:
        return 8


    elif humidity < 20:
        return 6


    else:
        return 3



def make_decision(
    sensor,
    visual,
    fusion,
    control,
    safety,
    history
):

    reasoning = []


    # ======================
    # 1. 安全最高优先级
    # ======================

    if not safety.get(
        "automatic_watering_allowed",
        True
    ):

        return {

            "action":
                "block_water",

            "water_sec":
                0,

            "reasoning":[
                "自动浇水被安全策略禁止"
            ],

            "confidence":
                0.95
        }



    fusion_risk = fusion.get(
        "risk"
    )


    humidity = sensor.get(
        "message",
        ""
    )


    soil_value = None


    try:

        soil_value = float(
            sensor.get(
                "message"
            )
            .split("湿度")[1]
            .split("%")[0]
        )

    except:

        pass



    strategy = control.get(
        "current_strategy"
    )



    # ======================
    # 2. 严重缺水
    # 不受cooldown限制
    # ======================

    if fusion_risk == "dry":


        water_sec = calculate_water_time(
            soil_value
        )


        return {

            "action":
                "water",

            "water_sec":
                water_sec,


            "reasoning":[

                sensor["message"],

                visual["message"],

                "综合判断植物存在缺水风险",

                f"执行补水{water_sec}秒"

            ],

            "confidence":
                0.88

        }




    # ======================
    # 3. cooldown观察
    # 只阻止普通补水
    # ======================

    if strategy == "cooldown_observe":


        return {


            "action":
                "observe",


            "water_sec":
                0,


            "reasoning":[


                "当前处于浇水冷却观察期",


                sensor["message"],


                visual["message"],


                history["message"],


                "暂不重复浇水，等待下一周期"

            ],


            "confidence":
                0.9

        }




    # ======================
    # 4. 轻度异常
    # ======================


    if fusion_risk == "attention":


        return {


            "action":
                "observe",


            "water_sec":
                0,


            "reasoning":[


                sensor["message"],


                visual["message"],


                "存在轻度风险",

                "继续观察湿度变化"

            ],


            "confidence":
                0.8

        }




    # ======================
    # 5. 严重视觉异常
    # ======================

    if fusion_risk == "warning":


        return {


            "action":
                "manual_check",


            "water_sec":
                0,


            "reasoning":[


                sensor["message"],


                visual["message"],


                "视觉状态异常，需要人工检查"

            ],


            "confidence":
                0.75

        }



    # ======================
    # 6. 正常状态
    # ======================


    return {


        "action":
            "observe",


        "water_sec":
            0,


        "reasoning":[


            "土壤状态正常",


            "视觉状态正常",


            history["message"],


            "继续观察"

        ],


        "confidence":
            0.9

    }



def reasoning(status):


    sensor_result = analyze_sensor(
        status.get(
            "sensor",
            {}
        )
    )


    visual_result = analyze_visual(
        status.get(
            "visual",
            {}
        )
    )


    fusion_result = analyze_fusion(

        sensor_result,

        visual_result

    )


    control_result = analyze_control(

        status.get(
            "control",
            {}
        )

    )


    history_result = analyze_history(

        status.get(
            "history",
            {}
        )

    )

    human_event_result = analyze_human_events(
        status.get("human_events", {})
    )
    manual_check_result = analyze_manual_check(
        status.get("human_events", {})
    )


    safety = status.get(
        "safety",
        {}
    )



    decision = make_decision(

        sensor_result,

        visual_result,

        fusion_result,

        control_result,

        safety,

        history_result

    )

    # Human records are audit facts used by explanations only.  They are appended
    # after the decision has been made, so action/water_sec cannot be affected.
    if human_event_result["last_external_watering"] is not None:
        decision["reasoning"] = list(decision["reasoning"]) + [human_event_result["message"]]
    if manual_check_result["last_manual_check"] is not None:
        decision["reasoning"] = list(decision["reasoning"]) + [manual_check_result["message"]]



    return {


        "device_code":

            status["device_code"],


        "generated_at":

            datetime.now().isoformat(),



        "observation":[

            sensor_result["message"],

            visual_result["message"],

            fusion_result["message"],

            history_result["message"]

            ,human_event_result["message"]

            ,manual_check_result["message"]

        ],



        "analysis":{


            "sensor":

                sensor_result,


            "visual":

                visual_result,


            "fusion":

                fusion_result,


            "control":

                control_result,


            "safety":

                safety

            ,"human_events": human_event_result

            ,"manual_check": manual_check_result

        },



        "decision":{


            "action":

                decision["action"],


            "water_sec":

                decision["water_sec"],


            "reasoning":

                decision["reasoning"]

        },


        "confidence":

            decision["confidence"]

    }





def main():

    import sys


    device_code = sys.argv[1]


    status = load_status(
        device_code
    )


    result = reasoning(
        status
    )


    output = (

        INPUT_DIR /

        f"{device_code}_decision.json"

    )


    output.write_text(

        json.dumps(

            result,

            ensure_ascii=False,

            indent=2

        ),

        encoding="utf-8"

    )



    print(

        json.dumps(

            result,

            ensure_ascii=False,

            indent=2

        )

    )


    print(

        f"\n[OK] Saved: {output}"

    )




if __name__ == "__main__":

    main()
