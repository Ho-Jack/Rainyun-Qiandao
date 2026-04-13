"""
服务器管理模块
负责服务器到期检查、自动续费等业务逻辑
"""
import logging
import os
from datetime import datetime
from typing import Optional

from config import DEFAULT_RENEW_COST_7_DAYS
from api_client import RainyunAPI, RainyunAPIError

logger = logging.getLogger(__name__)


class ServerInfo:
    """服务器信息"""

    def __init__(self, server_id: int, name: str, expired_at: int, renew_price: int = DEFAULT_RENEW_COST_7_DAYS):
        self.id = server_id
        self.name = name
        self.expired_at = expired_at  # Unix 时间戳
        self.renew_price = renew_price  # 续费 7 天所需积分

    @property
    def expired_datetime(self) -> datetime:
        """到期时间（datetime 对象）"""
        return datetime.fromtimestamp(self.expired_at)

    @property
    def days_remaining(self) -> int:
        """剩余天数"""
        delta = self.expired_datetime - datetime.now()
        return max(0, delta.days)

    @property
    def expired_str(self) -> str:
        """到期时间格式化字符串"""
        return self.expired_datetime.strftime("%Y-%m-%d %H:%M:%S")


class ServerManager:
    """服务器管理器"""

    def __init__(self, api_key: str):
        """
        初始化服务器管理器

        Args:
            api_key: 雨云 API 密钥
        """
        self.api = RainyunAPI(api_key)
        # 从环境变量读取配置
        self.auto_renew = os.environ.get("AUTO_RENEW", "true").lower() == "true"
        # 修复：RENEW_THRESHOLD_DAYS 类型错误时给出明确提示
        try:
            self.renew_threshold = int(os.environ.get("RENEW_THRESHOLD_DAYS", "7"))
        except ValueError:
            logger.error("配置错误：RENEW_THRESHOLD_DAYS 必须是整数，使用默认值 7")
            self.renew_threshold = 7

        # 白名单模式：只续费指定的产品ID（逗号分隔，为空则续费所有）
        renew_ids_str = os.environ.get("RENEW_PRODUCT_IDS", "").strip()
        self._whitelist_parse_error = False  # 标记白名单解析是否失败
        if renew_ids_str:
            try:
                self.renew_product_ids = [int(x.strip()) for x in renew_ids_str.split(",") if x.strip()]
                if self.renew_product_ids:
                    logger.info(f"白名单模式：只续费产品 {self.renew_product_ids}")
                else:
                    logger.info("白名单为空，将续费所有服务器")
            except ValueError:
                logger.error("配置错误：RENEW_PRODUCT_IDS 格式无效，应为逗号分隔的数字，自动续费已禁用")
                self.renew_product_ids = []
                self._whitelist_parse_error = True  # 解析失败时禁用自动续费
        else:
            self.renew_product_ids = []  # 空列表表示续费所有

    def get_all_servers(self) -> list:
        """
        获取所有服务器信息

        Returns:
            ServerInfo 对象列表
        """
        servers = []
        try:
            server_ids = self.api.get_server_ids()
            logger.info(f"找到 {len(server_ids)} 台服务器")

            for sid in server_ids:
                try:
                    detail = self.api.get_server_detail(sid)
                    # API 返回格式：{"Data": {"ExpDate": 1770306863, ...}, "RenewPointPrice": {"7": 2258, "31": 10000}}
                    server_data = detail.get("Data", {})
                    expired_at = server_data.get("ExpDate", 0)
                    # 修复：ExpDate 缺失或无效时跳过该服务器，避免误续费
                    if not expired_at or expired_at <= 0:
                        logger.warning(f"服务器 {sid} 的 ExpDate 无效 ({expired_at})，跳过")
                        continue
                    # 服务器名：尝试从 EggType 获取，否则用默认名
                    # 注意：EggType 可能为 null，需要安全处理
                    egg_type = server_data.get("EggType") or {}
                    egg_info = egg_type.get("egg") or {}
                    server_name = egg_info.get("title", f"游戏云-{sid}")
                    # 获取续费价格（动态获取，兜底使用默认值）
                    # 注意：API 返回的 key 可能是整数 7 或字符串 "7"，value 也可能是字符串
                    renew_price_map = detail.get("RenewPointPrice") or {}
                    raw_price = renew_price_map.get(7) or renew_price_map.get("7")
                    try:
                        renew_price = int(raw_price) if raw_price is not None else DEFAULT_RENEW_COST_7_DAYS
                    except (ValueError, TypeError):
                        logger.warning(f"服务器 {sid} 的续费价格无效 ({raw_price})，使用默认值 {DEFAULT_RENEW_COST_7_DAYS}")
                        renew_price = DEFAULT_RENEW_COST_7_DAYS
                    server = ServerInfo(
                        server_id=sid,
                        name=server_name,
                        expired_at=expired_at,
                        renew_price=renew_price
                    )
                    servers.append(server)
                    logger.info(f"  - {server.name} (ID:{sid}): 到期 {server.expired_str}, 剩余 {server.days_remaining} 天, 续费 {renew_price} 积分/7天")
                except RainyunAPIError as e:
                    logger.error(f"获取服务器 {sid} 详情失败: {e}")

        except RainyunAPIError as e:
            logger.error(f"获取服务器列表失败: {e}")

        return servers

    def check_and_renew(self) -> dict:
        """
        检查所有服务器到期时间，必要时自动续费

        Returns:
            结果摘要字典：
            {
                "points": 当前积分,
                "servers": [服务器状态列表],
                "renewed": [续费成功的服务器],
                "renew_failed": [续费失败的服务器或原因],
                "warnings": [警告信息],
                "points_warning": 积分预警信息（如果有）,
                "check_error": 接口检查失败原因（如果有）
            }
        """
        result = {
            "points": 0,
            "servers": [],
            "renewed": [],
            "renew_failed": [],
            "warnings": [],
            "points_warning": None,
            "check_error": None,
        }

        try:
            # 获取当前积分
            result["points"] = self.api.get_user_points()
            logger.info(f"当前积分: {result['points']}")

            # 获取所有服务器
            servers = self.get_all_servers()

            # 积分预警：计算白名单服务器续费所需总积分
            # 注意：白名单解析失败时跳过预警（因为自动续费已禁用，预警无意义）
            if not self._whitelist_parse_error:
                whitelist_servers = []
                if self.renew_product_ids:
                    # 有白名单，只计算白名单内的
                    whitelist_servers = [s for s in servers if s.id in self.renew_product_ids]
                else:
                    # 没有白名单，计算所有服务器
                    whitelist_servers = servers

                if whitelist_servers:
                    total_renew_cost = sum(s.renew_price for s in whitelist_servers)
                    if result["points"] < total_renew_cost:
                        shortage = total_renew_cost - result["points"]
                        days_needed = (shortage // 500) + (1 if shortage % 500 else 0)
                        result["points_warning"] = {
                            "current": result["points"],
                            "needed": total_renew_cost,
                            "shortage": shortage,
                            "servers_count": len(whitelist_servers),
                            "days_to_recover": days_needed
                        }
                        logger.warning(f"⚠️ 积分预警！当前 {result['points']}，续费所需 {total_renew_cost}，缺口 {shortage}")

            for server in servers:
                server_status = {
                    "id": server.id,
                    "name": server.name,
                    "expired": server.expired_str,
                    "days_remaining": server.days_remaining,
                    "renew_price": server.renew_price,
                    "renewed": False
                }

                # 检查是否需要续费
                if server.days_remaining <= self.renew_threshold:
                    logger.warning(f"⚠️ {server.name} 即将到期！剩余 {server.days_remaining} 天")

                    # 白名单解析错误时禁用自动续费，避免误操作
                    if self._whitelist_parse_error:
                        result["warnings"].append(f"{server.name} 即将到期，但白名单配置错误，自动续费已禁用")
                    # 白名单检查：如果设置了白名单，只续费白名单内的产品
                    elif self.renew_product_ids and server.id not in self.renew_product_ids:
                        logger.info(f"  ↳ 跳过：不在白名单中 (ID: {server.id})")
                        result["warnings"].append(f"{server.name} 即将到期，但不在续费白名单中")
                    elif self.auto_renew:
                        # 检查积分是否足够（使用动态价格）
                        if result["points"] >= server.renew_price:
                            try:
                                self.api.renew_server(server.id, days=7)
                                logger.info(f"✅ {server.name} 续费成功！消耗 {server.renew_price} 积分")
                                result["points"] -= server.renew_price
                                server_status["renewed"] = True
                                result["renewed"].append(server.name)
                            except RainyunAPIError as e:
                                logger.error(f"❌ {server.name} 续费失败: {e}")
                                result["renew_failed"].append(f"{server.name} 续费失败: {e}")
                                result["warnings"].append(f"{server.name} 续费失败: {e}")
                        else:
                            warning = f"积分不足！{server.name} 需要 {server.renew_price}，当前 {result['points']}"
                            logger.warning(warning)
                            result["renew_failed"].append(warning)
                            result["warnings"].append(warning)
                    else:
                        result["warnings"].append(f"{server.name} 即将到期，但自动续费已关闭")

                result["servers"].append(server_status)

        except RainyunAPIError as e:
            logger.error(f"服务器检查失败: {e}")
            result["check_error"] = str(e)
            result["warnings"].append(f"API 调用失败: {e}")

        return result

    def generate_report(self, result: dict) -> str:
        """
        生成服务器状态报告（用于通知推送）

        Args:
            result: check_and_renew 返回的结果字典

        Returns:
            格式化的报告字符串
        """
        lines = [
            "━━━━━━ 服务器状态 ━━━━━━",
            f"💰 当前积分: {result['points']}"
        ]

        # 积分预警（放在最前面，醒目提示）
        if result.get("points_warning"):
            pw = result["points_warning"]
            lines.append("")
            lines.append("🚨 积分预警 🚨")
            lines.append(f"   续费 {pw['servers_count']} 台服务器需要: {pw['needed']} 积分")
            lines.append(f"   当前积分: {pw['current']}")
            lines.append(f"   缺口: {pw['shortage']} 积分")
            lines.append(f"   建议: 连续签到 {pw['days_to_recover']} 天可补足")

        if result["servers"]:
            lines.append("")
            for s in result["servers"]:
                status = "✅ 已续费" if s["renewed"] else ""
                days_emoji = "🔴" if s["days_remaining"] <= 3 else "🟡" if s["days_remaining"] <= 7 else "🟢"
                lines.append(f"🖥️ {s['name']} (续费: {s['renew_price']}积分/7天)")
                lines.append(f"   {days_emoji} 剩余 {s['days_remaining']} 天 ({s['expired']}) {status}")
        else:
            lines.append("📭 无服务器")

        if result["renewed"]:
            lines.append("")
            lines.append(f"🎉 本次续费: {', '.join(result['renewed'])}")

        if result["warnings"]:
            lines.append("")
            lines.append("⚠️ 警告:")
            for w in result["warnings"]:
                lines.append(f"   - {w}")

        return "\n".join(lines)
