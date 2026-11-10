"""
Hard risk gates. A candidate must pass ALL of these before it's ever sent
to Telegram for approval. This keeps you from being pinged with trades
that would blow past your own limits.

Now also enforces the dynamic guardrails.Guardrails ban list as a hard
gate (defense in depth — scanner.py already filters banned symbols out
before this runs, but a hard hall-of-shame check here means a banned
symbol can never sneak through even if scanner-level filtering is ever
bypassed or refactored).
"""

import logging
from datetime import date

log = logging.getLogger("risk")

DEFAULT_SECTOR_MAP = {
    "NVDA": "Technology", "AAPL": "Technology", "MSFT": "Technology", "AMZN": "Consumer",
    "GOOGL": "Communication", "GOOG": "Communication", "AVGO": "Technology", "META": "Communication",
    "TSLA": "Consumer", "BRK.B": "Financials", "WMT": "Consumer", "LLY": "Healthcare",
    "JPM": "Financials", "XOM": "Energy", "JNJ": "Healthcare", "V": "Financials",
    "MA": "Financials", "COST": "Consumer", "ORCL": "Technology", "NFLX": "Consumer",
    "CVX": "Energy", "MU": "Technology", "ABBV": "Healthcare", "PLTR": "Technology",
    "BAC": "Financials", "AMD": "Technology", "PG": "Consumer Staples", "CAT": "Industrials",
    "HD": "Consumer", "KO": "Consumer", "CSCO": "Technology", "GE": "Industrials",
    "MRK": "Healthcare", "AMAT": "Technology", "LRCX": "Technology", "MS": "Financials",
    "RTX": "Industrials", "PM": "Consumer Staples", "GS": "Financials", "WFC": "Financials",
    "UNH": "Healthcare", "GEV": "Industrials", "TMUS": "Communication", "LIN": "Materials",
    "IBM": "Technology", "INTC": "Technology", "MCD": "Consumer", "PEP": "Consumer",
    "VZ": "Communication", "AXP": "Financials", "T": "Communication", "C": "Financials",
    "KLAC": "Technology", "NEE": "Utilities", "AMGN": "Healthcare", "TMO": "Healthcare",
    "ABT": "Healthcare", "TJX": "Consumer", "TXN": "Technology", "CRM": "Technology",
    "DIS": "Communication", "GILD": "Healthcare", "SCHW": "Financials", "ISRG": "Healthcare",
    "PFE": "Healthcare", "BA": "Industrials", "ANET": "Technology", "APH": "Technology",
    "COP": "Energy", "ADI": "Technology", "DE": "Industrials", "BLK": "Financials",
    "UBER": "Industrials", "UNP": "Industrials", "HON": "Industrials", "ETN": "Industrials",
    "LMT": "Industrials", "QCOM": "Technology", "WELL": "Real Estate", "APP": "Technology",
    "DHR": "Healthcare", "BKNG": "Consumer", "LOW": "Consumer", "PANW": "Technology",
    "SPGI": "Financials", "CB": "Financials", "SYK": "Healthcare", "PLD": "Real Estate",
    "BMY": "Healthcare", "ACN": "Technology", "INTU": "Technology", "NEM": "Materials",
    "GLW": "Technology", "PGR": "Financials", "PH": "Industrials", "COF": "Financials",
    "VRTX": "Healthcare", "MDT": "Healthcare", "NOW": "Technology", "MO": "Consumer Staples",
    "CME": "Financials", "SO": "Utilities", "HCA": "Healthcare", "DELL": "Technology",
    "MCK": "Healthcare", "SBUX": "Consumer", "DUK": "Utilities", "CMCSA": "Communication",
    "CEG": "Utilities", "CRWD": "Technology", "ADBE": "Technology", "VRT": "Industrials",
    "NOC": "Industrials", "EQIX": "Real Estate", "SNDK": "Technology", "WDC": "Technology",
    "GD": "Industrials", "BSX": "Healthcare", "HWM": "Industrials", "WM": "Industrials",
    "TT": "Industrials", "CVS": "Healthcare", "BX": "Financials", "ICE": "Financials",
    "STX": "Technology", "WMB": "Energy", "MAR": "Consumer", "FCX": "Materials",
    "FDX": "Industrials", "PNC": "Financials", "UPS": "Industrials", "MRSH": "Financials",
    "PWR": "Industrials", "KKR": "Financials", "BK": "Financials", "ADP": "Technology",
    "REGN": "Healthcare", "USB": "Financials", "JCI": "Industrials", "AMT": "Real Estate",
    "SHW": "Materials", "MCO": "Financials", "CDNS": "Technology", "SNPS": "Technology",
    "CSX": "Industrials", "ORLY": "Consumer", "ABNB": "Consumer", "MMM": "Industrials",
    "CMI": "Industrials", "ECL": "Materials", "SLB": "Energy", "RCL": "Consumer",
    "ITW": "Industrials", "EOG": "Energy", "EMR": "Industrials", "MDLZ": "Consumer Staples",
    "KMI": "Energy", "MSI": "Technology", "VLO": "Energy", "CI": "Healthcare",
    "MNST": "Consumer Staples", "CRH": "Materials", "AEP": "Utilities", "PSX": "Energy",
    "MPC": "Energy", "HLT": "Consumer", "ROST": "Consumer", "AON": "Financials",
    "NKE": "Consumer", "WBD": "Communication", "CL": "Consumer Staples", "CTAS": "Industrials",
    "GM": "Consumer", "RSG": "Industrials", "TDG": "Industrials", "DASH": "Consumer",
    "APO": "Financials", "APD": "Materials", "LHX": "Industrials", "NSC": "Industrials",
    "ELV": "Healthcare", "HOOD": "Financials", "SRE": "Utilities", "TRV": "Financials",
    "OXY": "Energy", "DLR": "Real Estate", "TEL": "Technology", "COR": "Healthcare",
    "PCAR": "Industrials", "SPG": "Real Estate", "FTNT": "Technology", "BKR": "Energy",
    "TFC": "Financials", "O": "Real Estate", "AFL": "Financials", "CIEN": "Technology",
    "CTVA": "Materials", "OKE": "Energy", "AJG": "Financials", "AZO": "Consumer",
    "TGT": "Consumer", "MPWR": "Technology", "D": "Utilities", "FANG": "Energy",
    "TRGP": "Energy", "ALL": "Financials", "FAST": "Industrials", "LITE": "Technology",
    "VST": "Utilities", "ETR": "Utilities", "GWW": "Industrials", "EA": "Communication",
    "ADSK": "Technology", "KEYS": "Technology", "ZTS": "Healthcare", "EXC": "Utilities",
    "NXPI": "Technology", "XEL": "Utilities", "CAH": "Healthcare", "AME": "Industrials",
    "FIX": "Industrials", "NDAQ": "Financials", "TER": "Technology", "PSA": "Real Estate",
    "CARR": "Industrials", "COIN": "Financials", "EW": "Healthcare", "F": "Consumer",
    "IDXX": "Healthcare", "URI": "Industrials", "MET": "Financials", "COHR": "Technology",
    "CVNA": "Consumer", "GRMN": "Consumer", "BDX": "Healthcare", "KR": "Consumer Staples",
    "DAL": "Industrials", "YUM": "Consumer", "WAB": "Industrials", "DDOG": "Technology",
    "FITB": "Financials", "HSY": "Consumer Staples", "CMG": "Consumer", "PYPL": "Financials",
    "ODFL": "Industrials", "EBAY": "Consumer", "ED": "Utilities", "PEG": "Utilities",
    "AIG": "Financials", "ROK": "Industrials", "AMP": "Financials", "CBRE": "Real Estate",
    "DHI": "Consumer", "MSCI": "Financials", "NUE": "Materials", "EQT": "Energy",
    "VTR": "Real Estate", "PCG": "Utilities", "WEC": "Utilities", "HIG": "Financials",
    "TTWO": "Communication", "ROP": "Technology", "XYZ": "Other", "CCL": "Consumer",
    "LYV": "Communication", "LVS": "Consumer", "VMC": "Materials", "KDP": "Consumer Staples",
    "CCI": "Real Estate", "STT": "Financials", "MLM": "Materials", "MCHP": "Technology",
    "ADM": "Consumer Staples", "AXON": "Industrials", "SATS": "Communication", "ACGL": "Financials",
    "SYY": "Consumer Staples", "PRU": "Financials", "WDAY": "Technology", "EME": "Industrials",
    "RMD": "Healthcare", "PAYX": "Technology", "KVUE": "Consumer Staples", "GEHC": "Healthcare",
    "TPL": "Energy", "A": "Healthcare", "CPRT": "Industrials", "HBAN": "Financials",
    "KMB": "Consumer Staples", "HAL": "Energy", "HPE": "Technology", "IR": "Industrials",
    "MTB": "Financials", "NRG": "Utilities", "IRM": "Real Estate", "ATO": "Utilities",
    "DVN": "Energy", "AEE": "Utilities", "DTE": "Utilities", "IBKR": "Financials",
    "UAL": "Industrials", "OTIS": "Industrials", "FISV": "Financials", "CTSH": "Technology",
    "XYL": "Industrials", "VICI": "Real Estate", "CBOE": "Financials", "FE": "Utilities",
    "WAT": "Healthcare", "DOW": "Materials", "TPR": "Consumer", "PPL": "Utilities",
    "JBL": "Technology", "IQV": "Healthcare", "EXPE": "Consumer", "RJF": "Financials",
    "EIX": "Utilities", "CNP": "Utilities", "TDY": "Technology", "DOV": "Industrials",
    "EXR": "Real Estate", "WTW": "Financials", "BIIB": "Healthcare", "CHTR": "Communication",
    "STLD": "Materials", "AWK": "Utilities", "KHC": "Consumer Staples", "ROL": "Industrials",
    "VRSK": "Industrials", "DG": "Consumer", "HUBB": "Industrials", "EL": "Consumer Staples",
    "ES": "Utilities", "STZ": "Consumer Staples", "EXE": "Healthcare", "CTRA": "Energy",
    "NTRS": "Financials", "CFG": "Financials", "MTD": "Healthcare", "FICO": "Technology",
    "LYB": "Materials", "ARES": "Financials", "BG": "Consumer Staples", "WRB": "Financials",
    "Q": "Technology", "ON": "Technology", "CINF": "Financials", "FIS": "Financials",
    "DXCM": "Healthcare", "TSCO": "Consumer", "CMS": "Utilities", "PPG": "Materials",
    "SYF": "Financials", "AVB": "Real Estate", "ULTA": "Consumer", "VRSN": "Technology",
    "DRI": "Consumer", "PHM": "Consumer", "RF": "Financials", "NI": "Utilities",
    "TSN": "Consumer Staples", "LH": "Healthcare", "BRO": "Financials", "EQR": "Real Estate",
    "CHD": "Consumer Staples", "EFX": "Industrials", "DGX": "Healthcare", "DLTR": "Consumer",
    "LEN": "Consumer", "VLTO": "Industrials", "STE": "Healthcare", "KEY": "Financials",
    "L": "Financials", "ALB": "Materials", "OMC": "Communication", "FSLR": "Technology",
    "WSM": "Consumer", "RL": "Consumer", "HUM": "Healthcare", "SW": "Technology",
    "NTAP": "Technology", "MRNA": "Healthcare", "JBHT": "Industrials", "CHRW": "Industrials",
    "GIS": "Consumer Staples", "TROW": "Financials", "CPAY": "Financials", "CF": "Materials",
    "LDOS": "Technology", "PFG": "Financials", "IP": "Materials", "DD": "Materials",
    "EVRG": "Utilities", "BR": "Technology", "SNA": "Industrials", "PKG": "Materials",
    "EXPD": "Industrials", "INCY": "Healthcare", "LUV": "Industrials", "GPN": "Financials",
    "IFF": "Materials", "LNT": "Utilities", "AMCR": "Materials", "NVR": "Consumer",
    "SBAC": "Real Estate", "WST": "Healthcare", "ZBH": "Healthcare", "WY": "Real Estate",
    "HPQ": "Technology", "LULU": "Consumer", "FTV": "Industrials", "PTC": "Technology",
    "CSGP": "Real Estate", "HOLX": "Healthcare", "AKAM": "Technology", "FFIV": "Technology",
    "CNC": "Healthcare", "LII": "Industrials", "BALL": "Materials", "TKO": "Communication",
    "VTRS": "Healthcare", "CDW": "Technology", "ESS": "Real Estate", "TRMB": "Technology",
    "TXT": "Industrials", "INVH": "Real Estate", "KIM": "Real Estate", "NDSN": "Industrials",
    "J": "Industrials", "HII": "Industrials", "PODD": "Healthcare", "GPC": "Consumer",
    "TYL": "Technology", "APA": "Energy", "MAA": "Real Estate", "DECK": "Consumer",
    "PNR": "Industrials", "IEX": "Industrials", "REG": "Real Estate", "SMCI": "Technology",
    "COO": "Healthcare", "BBY": "Consumer", "MKC": "Consumer Staples", "AVY": "Materials",
    "HST": "Real Estate", "EG": "Financials", "ERIE": "Financials", "HAS": "Consumer",
    "ALLE": "Industrials", "APTV": "Consumer", "CLX": "Consumer Staples", "HRL": "Consumer Staples",
    "BEN": "Financials", "MAS": "Industrials", "ALGN": "Healthcare", "BF.B": "Consumer Staples",
    "DPZ": "Consumer", "PNW": "Utilities", "FOX": "Communication", "FOXA": "Communication",
    "GNRC": "Industrials", "DOC": "Real Estate", "GEN": "Technology", "JKHY": "Technology",
    "SOLV": "Healthcare", "IT": "Technology", "SWK": "Industrials", "UDR": "Real Estate",
    "GDDY": "Technology", "UHS": "Healthcare", "IVZ": "Financials", "GL": "Financials",
    "TTD": "Technology", "AIZ": "Financials", "WYNN": "Consumer", "DVA": "Healthcare",
    "SJM": "Consumer Staples", "ZBRA": "Technology", "CPT": "Real Estate", "PSKY": "Other",
    "AES": "Utilities", "RVTY": "Healthcare", "MGM": "Consumer", "NWSA": "Communication",
    "BLDR": "Industrials", "FRT": "Real Estate", "AOS": "Industrials", "BAX": "Healthcare",
    "NCLH": "Consumer", "CRL": "Healthcare", "HSIC": "Healthcare", "BXP": "Real Estate",
    "TECH": "Healthcare", "TAP": "Consumer Staples", "FDS": "Financials", "SWKS": "Technology",
    "ARE": "Real Estate", "MOS": "Materials", "POOL": "Consumer", "CAG": "Consumer Staples",
    "EPAM": "Technology", "CPB": "Consumer Staples", "NWS": "Communication", "ASML": "Technology",
    "ARM": "Technology", "SHOP": "Technology", "PDD": "Consumer", "MRVL": "Technology",
    "MELI": "Consumer", "FER": "Industrials", "ALNY": "Healthcare", "MSTR": "Technology",
    "CCEP": "Consumer Staples", "TRI": "Industrials", "INSM": "Healthcare", "ZS": "Technology",
    "TEAM": "Technology"
}
class RiskManager:
    def __init__(self, cfg: dict, db, guardrails=None, market_data=None):
        self.cfg = cfg["risk"]
        self.db = db
        self.guardrails = guardrails
        self.market_data = market_data
        self.max_leg_spread_pct = cfg.get("liquidity", {}).get("max_leg_spread_pct", 0.20)
        # Load sector map from config, fallback to DEFAULT_SECTOR_MAP
        self.sector_map = self.cfg.get("sector_map", DEFAULT_SECTOR_MAP)
        # Optionally allow override via config
        for k, v in self.cfg.get("sector_map_override", {}).items():
            self.sector_map[k.upper()] = v
        self.max_per_sector = self.cfg.get("max_positions_per_sector", 2)
        
    def passes_spread_gate(self, candidate: dict) -> bool:
        """Hard gate: reject if ANY leg's current spread exceeds
        max_leg_spread_pct. Fails OPEN (returns True) if market_data wasn't
        wired in or a lookup errors — this is a liquidity sanity check, not
        a hard risk cap like max_risk_per_trade_pct, so a data hiccup
        shouldn't block an otherwise-good, already-filtered candidate."""
        if not self.market_data or not self.max_leg_spread_pct:
            return True
        for leg in candidate.get("legs", []):
            occ = leg.get("occ_symbol")
            if not occ:
                continue
            try:
                spread_pct = self.market_data.leg_spread_pct(occ)
            except Exception:
                log.debug("Spread check failed for %s — allowing through (fail-open)", occ)
                continue
            if spread_pct is not None and spread_pct > self.max_leg_spread_pct:
                log.info("Rejecting %s (%s): leg %s spread %.0f%% exceeds %.0f%% cap",
                         candidate["symbol"], candidate["strategy"], occ,
                         spread_pct * 100, self.max_leg_spread_pct * 100)
                return False
        return True
    
    def _get_sector(self, symbol: str) -> str:
        """Return sector for a symbol; default to 'Unknown' if not mapped."""
        return self.sector_map.get(symbol.upper(), "Unknown")

    def passes_sector_limits(self, symbol: str, open_positions: list) -> bool:
        """Check if adding this symbol would exceed the per-sector position cap."""
        if self.max_per_sector <= 0:
            return True  # disabled if set to 0
        candidate_sector = self._get_sector(symbol)
        # Count how many open positions are in the same sector
        sector_count = 0
        for pos in open_positions:
            pos_sym = pos.get("symbol", "")
            if self._get_sector(pos_sym) == candidate_sector:
                sector_count += 1
        if sector_count >= self.max_per_sector:
            log.info("Rejecting %s: sector %s already at max (%d positions)",
                     symbol, candidate_sector, self.max_per_sector)
            return False
        return True
    
    def account_snapshot(self, client, account_hash) -> dict:
        resp = client.account_details(account_hash, fields="positions")
        resp.raise_for_status()
        data = resp.json()["securitiesAccount"]
        return {
            "net_liq": data["currentBalances"]["liquidationValue"],
            "cash": data["currentBalances"].get("cashBalance", 0),
            "day_trades_left": data.get("roundTrips", 0),
            "positions": data.get("positions", []),
        }

    def max_risk_dollars(self, net_liq: float) -> float:
        return net_liq * self.cfg["max_risk_per_trade_pct"]

    def passes_position_limits(self, symbol: str, open_positions: list) -> bool:
        if len(open_positions) >= self.cfg["max_concurrent_positions"]:
            log.info("Rejecting %s: max_concurrent_positions reached", symbol)
            return False
        same_symbol = [p for p in open_positions if p.get("symbol") == symbol]
        if len(same_symbol) >= self.cfg["max_positions_per_symbol"]:
            log.info("Rejecting %s: max_positions_per_symbol reached", symbol)
            return False
        return True

    def passes_daily_trade_cap(self) -> bool:
        today_count = self.db.count_trades_today(date.today().isoformat())
        return today_count < self.cfg["max_new_trades_per_day"]

    def passes_pdt_check(self, net_liq: float, is_day_trade: bool) -> bool:
        if not self.cfg.get("respect_pdt_rule", True):
            return True
        if net_liq >= 25000:
            return True
        # Under 25k: block anything that would open+close same-day (options
        # bought and sold same session count as a day trade). The scanner
        # only opens new positions, but this guards against same-day exits
        # triggered elsewhere (e.g. a manual close) counting against you.
        return not is_day_trade

    def passes_guardrails(self, candidate: dict) -> bool:
        if not self.guardrails:
            return True
        symbol = candidate["symbol"]
        strategy = candidate["strategy"]
        if self.guardrails.is_banned(symbol, strategy):
            log.info("Rejecting %s (%s): banned by guardrails (see guardrails.list_active_bans())", symbol, strategy)
            return False
        return True

    def size_candidate(self, candidate: dict, net_liq: float) -> dict:
        """Attach a suggested contract quantity capped by max risk per trade.

        If even ONE contract's max_loss_per_contract exceeds max_risk (the
        dollar cap implied by max_risk_per_trade_pct), suggested_qty is set
        to 0 rather than force-rounded up to 1. The old `max(1, ...)` here
        meant a candidate could get a "suggested" size that silently blew
        past your own risk cap — most visible on directional trades, where
        a single high-premium long option's max_loss_per_contract (premium
        x 100) can exceed max_risk outright on a small account. gate()
        below rejects qty=0 candidates rather than sending them for
        approval undersized-but-still-oversized.

        LEAPS are a special case: a single deep-ITM call can cost far more
        than max_risk_per_trade_pct (2% of net liq) allows, so sizing them
        off that cap would reject every LEAP on a normal account. LEAPS use
        their own per-position premium cap instead
        (risk.leaps.max_premium_pct_per_position).
        """
        per_contract_risk = candidate.get("max_loss_per_contract")
        if candidate.get("strategy") == "leaps":
            return self._size_leaps(candidate, net_liq, per_contract_risk)

        max_risk = self.max_risk_dollars(net_liq)
        if not per_contract_risk or per_contract_risk <= 0:
            candidate["suggested_qty"] = 1
            return candidate
        qty = int(max_risk // per_contract_risk)
        if qty < 1:
            candidate["suggested_qty"] = 0
            candidate["total_risk_at_suggested_qty"] = round(per_contract_risk, 2)
            candidate["rejected_reason"] = (
                f"1 contract risks ${per_contract_risk:.2f}, which exceeds max_risk "
                f"${max_risk:.2f} ({self.cfg['max_risk_per_trade_pct']:.1%} of ${net_liq:,.2f} net liq)"
            )
            return candidate
        candidate["suggested_qty"] = qty
        candidate["total_risk_at_suggested_qty"] = round(qty * per_contract_risk, 2)
        return candidate

    def _size_leaps(self, candidate: dict, net_liq: float, per_contract_risk) -> dict:
        """Size a LEAPS candidate by premium (not by max_risk_per_trade_pct).
        Max loss is the full premium — reported honestly, since approvals
        are human-tapped and options gap through a stop."""
        max_pct = self.cfg.get("leaps", {}).get("max_premium_pct_per_position", 0.10)
        max_dollars = net_liq * max_pct
        if not per_contract_risk or per_contract_risk <= 0:
            candidate["suggested_qty"] = 1
            return candidate
        qty = int(max_dollars // per_contract_risk)
        if qty < 1:
            candidate["suggested_qty"] = 0
            candidate["total_risk_at_suggested_qty"] = round(per_contract_risk, 2)
            candidate["rejected_reason"] = (
                f"1 contract costs ${per_contract_risk:.2f} in premium, which exceeds the "
                f"LEAPS per-position cap ${max_dollars:.2f} "
                f"({max_pct:.1%} of ${net_liq:,.2f} net liq)"
            )
            return candidate
        candidate["suggested_qty"] = qty
        candidate["total_risk_at_suggested_qty"] = round(qty * per_contract_risk, 2)
        return candidate

    def passes_sector_dollar_limit(self, candidate: dict, net_liq: float) -> bool:
        if candidate.get("strategy") == "leaps":
            max_pct = self.cfg.get("leaps", {}).get("max_risk_per_sector_pct", 0.15)
        else:
            max_pct = self.cfg.get("max_risk_per_sector_pct", 0.04)
        if max_pct <= 0:
            return True
        sector = self._get_sector(candidate["symbol"])
        committed = self.db.open_position_risk_by_sector(self.sector_map).get(sector, 0.0)
        candidate_risk = (candidate.get("max_loss_per_contract") or 0) * candidate.get("suggested_qty", 1)
        cap = net_liq * max_pct
        if committed + candidate_risk > cap:
            log.info("Rejecting %s: sector %s risk $%.2f + $%.2f would exceed $%.2f cap",
                      candidate["symbol"], sector, committed, candidate_risk, cap)
            return False
        return True

    def passes_leaps_exposure_limit(self, candidate: dict, net_liq: float) -> bool:
        """Cap total open LEAPS premium (all names) plus this candidate at
        net_liq * risk.leaps.max_total_leaps_pct. Non-LEAPS candidates pass
        through untouched."""
        if candidate.get("strategy") != "leaps":
            return True
        max_pct = self.cfg.get("leaps", {}).get("max_total_leaps_pct", 0.30)
        if max_pct <= 0:
            return True
        open_premium = self.db.open_leaps_premium()
        candidate_premium = (candidate.get("max_loss_per_contract") or 0) * candidate.get("suggested_qty", 1)
        cap = net_liq * max_pct
        if open_premium + candidate_premium > cap:
            log.info("Rejecting %s: total LEAPS premium $%.2f + $%.2f would exceed $%.2f cap",
                      candidate["symbol"], open_premium, candidate_premium, cap)
            return False
        return True

    def gate(self, candidate: dict, snapshot: dict) -> bool:
        if not self.passes_spread_gate(candidate):
            return False
        if candidate.get("suggested_qty", 1) < 1:
            log.info("Rejecting %s (%s): %s", candidate["symbol"], candidate["strategy"],
                      candidate.get("rejected_reason", "sized to 0 contracts — exceeds max_risk_per_trade_pct"))
            return False
        if not self.passes_guardrails(candidate):          # see fix 5 — now takes the whole candidate
            return False
        if not self.passes_sector_limits(candidate["symbol"], snapshot["positions"]):
            return False
        if not self.passes_sector_dollar_limit(candidate, snapshot["net_liq"]):
            return False
        if not self.passes_leaps_exposure_limit(candidate, snapshot["net_liq"]):
            return False
        if not self.passes_position_limits(candidate["symbol"], snapshot["positions"]):
            return False
        if not self.passes_daily_trade_cap():
            return False
        if not self.passes_pdt_check(snapshot["net_liq"], is_day_trade=False):
            return False
        return True