/**
 * FIX 4.2 Protocol Dictionary
 *
 * Client-side ES module for tag name lookups, enum translations,
 * and message formatting. Used by the UI to translate raw FIX
 * messages into human-readable form without round-tripping to
 * the server.
 */

// ---------------------------------------------------------------------------
// 1. FIX_TAGS  --  tag number (string) -> human-readable name
// ---------------------------------------------------------------------------

export const FIX_TAGS = {
    "1":   "Account",
    "2":   "AdvId",
    "3":   "AdvRefID",
    "4":   "AdvSide",
    "5":   "AdvTransType",
    "6":   "AvgPx",
    "7":   "BeginSeqNo",
    "8":   "BeginString",
    "9":   "BodyLength",
    "10":  "CheckSum",
    "11":  "ClOrdID",
    "12":  "Commission",
    "13":  "CommType",
    "14":  "CumQty",
    "15":  "Currency",
    "16":  "EndSeqNo",
    "17":  "ExecID",
    "18":  "ExecInst",
    "19":  "ExecRefID",
    "20":  "ExecTransType",
    "21":  "HandlInst",
    "22":  "SecurityIDSource",
    "23":  "IOIID",
    "24":  "IOIOthSvc",
    "25":  "IOIQltyInd",
    "26":  "IOIRefID",
    "27":  "IOIQty",
    "28":  "IOITransType",
    "29":  "LastCapacity",
    "30":  "LastMkt",
    "31":  "LastPx",
    "32":  "LastQty",
    "33":  "NoLinesOfText",
    "34":  "MsgSeqNum",
    "35":  "MsgType",
    "36":  "NewSeqNo",
    "37":  "OrderID",
    "38":  "OrderQty",
    "39":  "OrdStatus",
    "40":  "OrdType",
    "41":  "OrigClOrdID",
    "42":  "OrigTime",
    "43":  "PossDupFlag",
    "44":  "Price",
    "45":  "RefSeqNum",
    "46":  "RelatdSym",
    "47":  "Rule80A",
    "48":  "SecurityID",
    "49":  "SenderCompID",
    "50":  "SenderSubID",
    "51":  "SendingDate",
    "52":  "SendingTime",
    "53":  "Quantity",
    "54":  "Side",
    "55":  "Symbol",
    "56":  "TargetCompID",
    "57":  "TargetSubID",
    "58":  "Text",
    "59":  "TimeInForce",
    "60":  "TransactTime",
    "61":  "Urgency",
    "62":  "ValidUntilTime",
    "63":  "SettlType",
    "64":  "SettlDate",
    "65":  "SymbolSfx",
    "66":  "ListID",
    "67":  "ListSeqNo",
    "68":  "TotNoOrders",
    "69":  "ListExecInst",
    "70":  "AllocID",
    "71":  "AllocTransType",
    "72":  "RefAllocID",
    "73":  "NoOrders",
    "74":  "AvgPxPrecision",
    "75":  "TradeDate",
    "76":  "ExecBroker",
    "77":  "PositionEffect",
    "78":  "NoAllocs",
    "79":  "AllocAccount",
    "80":  "AllocQty",
    "81":  "ProcessCode",
    "82":  "NoRpts",
    "83":  "RptSeq",
    "84":  "CxlQty",
    "85":  "NoDlvyInst",
    "86":  "DlvyInst",
    "87":  "AllocStatus",
    "88":  "AllocRejCode",
    "89":  "Signature",
    "90":  "SecureDataLen",
    "91":  "SecureData",
    "92":  "BrokerOfCredit",
    "93":  "SignatureLength",
    "94":  "EmailType",
    "95":  "RawDataLength",
    "96":  "RawData",
    "97":  "PossResend",
    "98":  "EncryptMethod",
    "99":  "StopPx",
    "100": "ExDestination",
    "102": "CxlRejReason",
    "103": "OrdRejReason",
    "104": "IOIQualifier",
    "105": "WaveNo",
    "106": "Issuer",
    "107": "SecurityDesc",
    "108": "HeartBtInt",
    "109": "ClientID",
    "110": "MinQty",
    "111": "MaxFloor",
    "112": "TestReqID",
    "113": "ReportToExch",
    "114": "LocateReqd",
    "115": "OnBehalfOfCompID",
    "116": "OnBehalfOfSubID",
    "117": "QuoteID",
    "118": "NetMoney",
    "119": "SettlCurrAmt",
    "120": "SettlCurrency",
    "121": "ForexReq",
    "122": "OrigSendingTime",
    "123": "GapFillFlag",
    "124": "NoExecs",
    "125": "CxlType",
    "126": "ExpireTime",
    "127": "DKReason",
    "128": "DeliverToCompID",
    "129": "DeliverToSubID",
    "130": "IOINaturalFlag",
    "131": "QuoteReqID",
    "132": "BidPx",
    "133": "OfferPx",
    "134": "BidSize",
    "135": "OfferSize",
    "136": "NoMiscFees",
    "137": "MiscFeeAmt",
    "138": "MiscFeeCurr",
    "139": "MiscFeeType",
    "140": "PrevClosePx",
    "141": "ResetSeqNumFlag",
    "142": "SenderLocationID",
    "143": "TargetLocationID",
    "144": "OnBehalfOfLocationID",
    "145": "DeliverToLocationID",
    "146": "NoRelatedSym",
    "147": "Subject",
    "148": "Headline",
    "149": "URLLink",
    "150": "ExecType",
    "151": "LeavesQty",
    "152": "CashOrderQty",
    "153": "AllocAvgPx",
    "154": "AllocNetMoney",
    "155": "SettlCurrFxRate",
    "156": "SettlCurrFxRateCalc",
    "157": "NumDaysInterest",
    "158": "AccruedInterestRate",
    "159": "AccruedInterestAmt",
    "160": "SettlInstMode",
    "161": "AllocText",
    "162": "SettlInstID",
    "163": "SettlInstTransType",
    "164": "EmailThreadID",
    "165": "SettlInstSource",
    "166": "SettlLocation",
    "167": "SecurityType",
    "168": "EffectiveTime",
    "169": "StandInstDbType",
    "170": "StandInstDbName",
    "171": "StandInstDbID",
    "172": "SettlDeliveryType",
    "173": "SettlDepositoryCode",
    "174": "SettlBrkrCode",
    "175": "SettlInstCode",
    "176": "SecuritySettlAgentName",
    "177": "SecuritySettlAgentCode",
    "178": "SecuritySettlAgentAcctNum",
    "179": "SecuritySettlAgentAcctName",
    "180": "SecuritySettlAgentContactName",
    "181": "SecuritySettlAgentContactPhone",
    "182": "CashSettlAgentName",
    "183": "CashSettlAgentCode",
    "184": "CashSettlAgentAcctNum",
    "185": "CashSettlAgentAcctName",
    "186": "CashSettlAgentContactName",
    "187": "CashSettlAgentContactPhone",
    "188": "BidSpotRate",
    "189": "BidForwardPoints",
    "190": "OfferSpotRate",
    "191": "OfferForwardPoints",
    "192": "OrderQty2",
    "193": "SettlDate2",
    "194": "LastSpotRate",
    "195": "LastForwardPoints",
    "196": "AllocLinkID",
    "197": "AllocLinkType",
    "198": "SecondaryOrderID",
    "199": "NoIOIQualifiers",
    "200": "MaturityMonthYear",
    "201": "PutOrCall",
    "202": "StrikePrice",
    "203": "CoveredOrUncovered",
    "204": "CustomerOrFirm",
    "205": "MaturityDay",
    "206": "OptAttribute",
    "207": "SecurityExchange",
    "208": "NotifyBrokerOfCredit",
    "209": "AllocHandlInst",
    "210": "MaxShow",
    "211": "PegDifference",
    "212": "XmlDataLen",
    "213": "XmlData",
    "214": "SettlInstRefID",
    "215": "NoRoutingIDs",
    "216": "RoutingType",
    "217": "RoutingID",
    "218": "Spread",
    "219": "Benchmark",
    "220": "BenchmarkCurveCurrency",
    "221": "BenchmarkCurveName",
    "222": "BenchmarkCurvePoint",
    "223": "CouponRate",
    "224": "CouponPaymentDate",
    "225": "IssueDate",
    "226": "RepurchaseTerm",
    "227": "RepurchaseRate",
    "228": "Factor",
    "229": "TradeOriginationDate",
    "230": "ExDate",
    "231": "ContractMultiplier",
    "232": "NoStipulations",
    "233": "StipulationType",
    "234": "StipulationValue",
    "262": "MDReqID",
    "263": "SubscriptionRequestType",
    "264": "MarketDepth",
    "265": "MDUpdateType",
    "266": "AggregatedBook",
    "267": "NoMDEntryTypes",
    "268": "NoMDEntries",
    "269": "MDEntryType",
    "270": "MDEntryPx",
    "271": "MDEntrySize",
    "272": "MDEntryDate",
    "273": "MDEntryTime",
    "274": "TickDirection",
    "275": "MDMkt",
    "276": "QuoteCondition",
    "277": "TradeCondition",
    "278": "MDEntryID",
    "279": "MDUpdateAction",
    "280": "MDEntryRefID",
    "281": "MDReqRejReason",
    "282": "MDEntryOriginator",
    "283": "LocationID",
    "284": "DeskID",
    "285": "DeleteReason",
    "286": "OpenCloseSettlFlag",
    "287": "SellerDays",
    "288": "MDEntryBuyer",
    "289": "MDEntrySeller",
    "290": "MDEntryPositionNo",
    "291": "FinancialStatus",
    "292": "CorporateAction",
    "293": "DefBidSize",
    "294": "DefOfferSize",
    "295": "NoQuoteEntries",
    "296": "NoQuoteSets",
    "297": "QuoteStatus",
    "298": "QuoteCancelType",
    "299": "QuoteEntryID",
    "300": "QuoteRejectReason",
    "301": "QuoteResponseLevel",
    "302": "QuoteSetID",
    "303": "QuoteRequestType",
    "304": "TotQuoteEntries",
    "305": "UnderlyingSecurityIDSource",
    "306": "UnderlyingIssuer",
    "307": "UnderlyingSecurityDesc",
    "308": "UnderlyingSecurityExchange",
    "309": "UnderlyingSecurityID",
    "310": "UnderlyingSecurityType",
    "311": "UnderlyingSymbol",
    "312": "UnderlyingSymbolSfx",
    "313": "UnderlyingMaturityMonthYear",
    "314": "UnderlyingMaturityDay",
    "315": "UnderlyingPutOrCall",
    "316": "UnderlyingStrikePrice",
    "317": "UnderlyingOptAttribute",
    "318": "UnderlyingCurrency",
    "319": "RatioQty",
    "320": "SecurityReqID",
    "321": "SecurityRequestType",
    "322": "SecurityResponseID",
    "323": "SecurityResponseType",
    "324": "SecurityStatusReqID",
    "325": "UnsolicitedIndicator",
    "326": "SecurityTradingStatus",
    "327": "HaltReason",
    "328": "InViewOfCommon",
    "329": "DueToRelated",
    "330": "BuyVolume",
    "331": "SellVolume",
    "332": "HighPx",
    "333": "LowPx",
    "334": "Adjustment",
    "335": "TradSesReqID",
    "336": "TradingSessionID",
    "337": "ContraTrader",
    "338": "TradSesMethod",
    "339": "TradSesMode",
    "340": "TradSesStatus",
    "341": "TradSesStartTime",
    "342": "TradSesOpenTime",
    "343": "TradSesPreCloseTime",
    "344": "TradSesCloseTime",
    "345": "TradSesEndTime",
    "346": "NumberOfOrders",
    "347": "MessageEncoding",
    "348": "EncodedIssuerLen",
    "349": "EncodedIssuer",
    "350": "EncodedSecurityDescLen",
    "351": "EncodedSecurityDesc",
    "352": "EncodedListExecInstLen",
    "353": "EncodedListExecInst",
    "354": "EncodedTextLen",
    "355": "EncodedText",
    "356": "EncodedSubjectLen",
    "357": "EncodedSubject",
    "358": "EncodedHeadlineLen",
    "359": "EncodedHeadline",
    "360": "EncodedAllocTextLen",
    "361": "EncodedAllocText",
    "362": "EncodedUnderlyingIssuerLen",
    "363": "EncodedUnderlyingIssuer",
    "364": "EncodedUnderlyingSecurityDescLen",
    "365": "EncodedUnderlyingSecurityDesc",
    "366": "AllocPrice",
    "367": "QuoteSetValidUntilTime",
    "368": "QuoteEntryRejectReason",
    "369": "LastMsgSeqNumProcessed",
    "370": "OnBehalfOfSendingTime",
    "371": "RefTagID",
    "372": "RefMsgType",
    "373": "SessionRejectReason",
    "374": "BidRequestTransType",
    "375": "ContraBroker",
    "376": "ComplianceID",
    "377": "SolicitedFlag",
    "378": "ExecRestatementReason",
    "379": "BusinessRejectRefID",
    "380": "BusinessRejectReason",
    "381": "GrossTradeAmt",
    "382": "NoContraBrokers",
    "383": "MaxMessageSize",
    "384": "NoMsgTypes",
    "385": "MsgDirection",
    "386": "NoTradingSessions",
    "387": "TotalVolumeTraded",
    "388": "DiscretionInst",
    "389": "DiscretionOffset",
    "390": "BidID",
    "391": "ClientBidID",
    "392": "ListName",
    "393": "TotalNumSecurities",
    "394": "BidType",
    "395": "NumTickets",
    "396": "SideValue1",
    "397": "SideValue2",
    "398": "NoBidDescriptors",
    "399": "BidDescriptorType",
    "400": "BidDescriptor",
    "401": "SideValueInd",
    "402": "LiquidityPctLow",
    "403": "LiquidityPctHigh",
    "404": "LiquidityValue",
    "405": "EFPTrackingError",
    "406": "FairValue",
    "407": "OutsideIndexPct",
    "408": "ValueOfFutures",
    "409": "LiquidityIndType",
    "410": "WtAverageLiquidity",
    "411": "ExchangeForPhysical",
    "412": "OutMainCntryUIndex",
    "413": "CrossPercent",
    "414": "ProgRptReqs",
    "415": "ProgPeriodInterval",
    "416": "IncTaxInd",
    "417": "NumBidders",
    "418": "TradeType",
    "419": "BasisPxType",
    "420": "NoBidComponents",
    "421": "Country",
    "422": "TotNoStrikes",
    "423": "PriceType",
    "424": "DayOrderQty",
    "425": "DayCumQty",
    "426": "DayAvgPx",
    "427": "GTBookingInst",
    "428": "NoStrikes",
    "429": "ListStatusType",
    "430": "NetGrossInd",
    "431": "ListOrderStatus",
    "432": "ExpireDate",
    "433": "ListExecInstType",
    "434": "CxlRejResponseTo",
    "435": "UnderlyingCouponRate",
    "436": "UnderlyingContractMultiplier",
    "437": "ContraTradeQty",
    "438": "ContraTradeTime",
    "439": "ClearingFirm",
    "440": "ClearingAccount",
    "441": "LiquidityNumSecurities",
    "442": "MultiLegReportingType",
    "443": "StrikeTime",
    "444": "ListStatusText",
    "445": "EncodedListStatusTextLen",
    "446": "EncodedListStatusText",
};

// ---------------------------------------------------------------------------
// 2. FIX_ENUMS  --  tag number (string) -> { value: label }
// ---------------------------------------------------------------------------

export const FIX_ENUMS = {

    // Tag 4 - AdvSide
    "4": {
        "B": "Buy",
        "S": "Sell",
        "X": "Cross",
        "T": "Trade",
    },

    // Tag 5 - AdvTransType
    "5": {
        "N": "New",
        "C": "Cancel",
        "R": "Replace",
    },

    // Tag 20 - ExecTransType
    "20": {
        "0": "New",
        "1": "Cancel",
        "2": "Correct",
        "3": "Status",
    },

    // Tag 21 - HandlInst
    "21": {
        "1": "Automated, no intervention",
        "2": "Automated, intervention OK",
        "3": "Manual",
    },

    // Tag 27 - IOIQty
    "27": {
        "S": "Small",
        "M": "Medium",
        "L": "Large",
    },

    // Tag 28 - IOITransType
    "28": {
        "N": "New",
        "C": "Cancel",
        "R": "Replace",
    },

    // Tag 29 - LastCapacity
    "29": {
        "1": "Agent",
        "2": "Cross as agent",
        "3": "Cross as principal",
        "4": "Principal",
    },

    // Tag 35 - MsgType  (all FIX 4.2 message types)
    "35": {
        "0":  "Heartbeat",
        "1":  "Test Request",
        "2":  "Resend Request",
        "3":  "Reject",
        "4":  "Sequence Reset",
        "5":  "Logout",
        "6":  "Indication of Interest",
        "7":  "Advertisement",
        "8":  "Execution Report",
        "9":  "Order Cancel Reject",
        "A":  "Logon",
        "B":  "News",
        "C":  "Email",
        "D":  "New Order - Single",
        "E":  "New Order - List",
        "F":  "Order Cancel Request",
        "G":  "Order Cancel/Replace Request",
        "H":  "Order Status Request",
        "J":  "Allocation",
        "K":  "List Cancel Request",
        "L":  "List Execute",
        "M":  "List Status Request",
        "N":  "List Status",
        "P":  "Allocation ACK",
        "Q":  "Don't Know Trade",
        "R":  "Quote Request",
        "S":  "Quote",
        "T":  "Settlement Instructions",
        "V":  "Market Data Request",
        "W":  "Market Data - Snapshot/Full Refresh",
        "X":  "Market Data - Incremental Refresh",
        "Y":  "Market Data Request Reject",
        "Z":  "Quote Cancel",
        "a":  "Quote Status Request",
        "b":  "Mass Quote Acknowledgement",
        "c":  "Security Definition Request",
        "d":  "Security Definition",
        "e":  "Security Status Request",
        "f":  "Security Status",
        "g":  "Trading Session Status Request",
        "h":  "Trading Session Status",
        "i":  "Mass Quote",
        "j":  "Business Message Reject",
        "k":  "Bid Request",
        "l":  "Bid Response",
        "m":  "List Strike Price",
    },

    // Tag 39 - OrdStatus
    "39": {
        "0": "New",
        "1": "Partially Filled",
        "2": "Filled",
        "3": "Done for Day",
        "4": "Canceled",
        "5": "Replaced",
        "6": "Pending Cancel",
        "7": "Stopped",
        "8": "Rejected",
        "9": "Suspended",
        "A": "Pending New",
        "B": "Calculated",
        "C": "Expired",
        "D": "Accepted for Bidding",
        "E": "Pending Replace",
    },

    // Tag 40 - OrdType
    "40": {
        "1": "Market",
        "2": "Limit",
        "3": "Stop",
        "4": "Stop Limit",
        "5": "Market on Close",
        "6": "With or Without",
        "7": "Limit or Better",
        "8": "Limit with or Better",
        "9": "On Basis",
        "A": "On Close",
        "B": "Limit on Close",
        "C": "Forex - Market",
        "D": "Previously Quoted",
        "E": "Previously Indicated",
        "F": "Forex - Limit",
        "G": "Forex - Swap",
        "H": "Forex - Previously Quoted",
        "I": "Funari",
        "P": "Pegged",
    },

    // Tag 47 - Rule80A
    "47": {
        "A": "Agency single order",
        "B": "Short exempt transaction (B)",
        "C": "Program order, non-index arb, for member firm/org",
        "D": "Program order, index arb, for member firm/org",
        "E": "Short exempt transaction for principal",
        "F": "Short exempt transaction (F)",
        "H": "Short exempt transaction (H)",
        "I": "Individual investor, single order",
        "J": "Program order, index arb, for individual customer",
        "K": "Program order, non-index arb, for individual customer",
        "L": "Short exempt transaction for member competing market-maker affiliated with firm clearing trade",
        "M": "Program order, index arb, for other member",
        "N": "Program order, non-index arb, for other member",
        "O": "Proprietary transactions for competing market-maker that is affiliated with clearing member",
        "P": "Principal",
        "R": "Transactions for the account of a non-member competing market-maker",
        "S": "Specialist trades",
        "T": "Transactions for the account of an unaffiliated member's competing market-maker",
        "U": "Program order, index arb, for other agency",
        "W": "All other orders as agent for other member",
        "X": "Short exempt transaction for member competing market-maker not affiliated with firm clearing trade",
        "Y": "Program order, non-index arb, for other agency",
        "Z": "Short exempt transaction for non-member competing market-maker",
    },

    // Tag 54 - Side
    "54": {
        "1": "Buy",
        "2": "Sell",
        "3": "Buy Minus",
        "4": "Sell Plus",
        "5": "Sell Short",
        "6": "Sell Short Exempt",
        "7": "Undisclosed",
        "8": "Cross",
        "9": "Cross Short",
    },

    // Tag 59 - TimeInForce
    "59": {
        "0": "Day",
        "1": "Good Till Cancel (GTC)",
        "2": "At the Opening (OPG)",
        "3": "Immediate or Cancel (IOC)",
        "4": "Fill or Kill (FOK)",
        "5": "Good Till Crossing (GTX)",
        "6": "Good Till Date",
    },

    // Tag 61 - Urgency
    "61": {
        "0": "Normal",
        "1": "Flash",
        "2": "Background",
    },

    // Tag 63 - SettlType
    "63": {
        "0": "Regular",
        "1": "Cash",
        "2": "Next Day",
        "3": "T+2",
        "4": "T+3",
        "5": "T+4",
        "6": "Future",
        "7": "When Issued",
        "8": "Sellers Option",
        "9": "T+5",
    },

    // Tag 71 - AllocTransType
    "71": {
        "0": "New",
        "1": "Replace",
        "2": "Cancel",
        "3": "Preliminary",
        "4": "Calculated",
        "5": "Calculated without Preliminary",
    },

    // Tag 77 - PositionEffect
    "77": {
        "O": "Open",
        "C": "Close",
        "R": "Rolled",
        "F": "FIFO",
    },

    // Tag 87 - AllocStatus
    "87": {
        "0": "Accepted",
        "1": "Block Level Reject",
        "2": "Account Level Reject",
        "3": "Received",
    },

    // Tag 98 - EncryptMethod
    "98": {
        "0": "None",
        "1": "PKCS",
        "2": "DES (ECB)",
        "3": "PKCS/DES (EDE)",
        "4": "PGP/DES (MD5)",
        "5": "PGP/DES (SHA-1)",
        "6": "PGP/RSA (MD5)",
    },

    // Tag 102 - CxlRejReason
    "102": {
        "0": "Too Late to Cancel",
        "1": "Unknown Order",
        "2": "Broker/Exchange Option",
        "3": "Order Already in Pending Cancel or Pending Replace Status",
    },

    // Tag 103 - OrdRejReason
    "103": {
        "0":  "Broker/Exchange Option",
        "1":  "Unknown Symbol",
        "2":  "Exchange Closed",
        "3":  "Order Exceeds Limit",
        "4":  "Too Late to Enter",
        "5":  "Unknown Order",
        "6":  "Duplicate Order",
        "7":  "Duplicate of a Verbally Communicated Order",
        "8":  "Stale Order",
        "9":  "Trade Along Required",
        "10": "Invalid Investor ID",
        "11": "Unsupported Order Characteristic",
        "12": "Surveillance Option",
    },

    // Tag 104 - IOIQualifier
    "104": {
        "A": "All or None",
        "C": "At the Close",
        "I": "In Touch With",
        "L": "Limit",
        "M": "More Behind",
        "O": "At the Open",
        "P": "Taking a Position",
        "Q": "At the Market",
        "R": "Ready to Trade",
        "S": "Portfolio Shown",
        "T": "Through the Day",
        "V": "Versus",
        "W": "Indication - Working Away",
        "X": "Crossing Opportunity",
        "Y": "At the Midpoint",
        "Z": "Pre-open",
    },

    // Tag 127 - DKReason
    "127": {
        "A": "Unknown Symbol",
        "B": "Wrong Side",
        "C": "Quantity Exceeds Order",
        "D": "No Matching Order",
        "E": "Price Exceeds Limit",
        "Z": "Other",
    },

    // Tag 150 - ExecType
    "150": {
        "0": "New",
        "1": "Partial Fill",
        "2": "Fill",
        "3": "Done for Day",
        "4": "Canceled",
        "5": "Replaced",
        "6": "Pending Cancel",
        "7": "Stopped",
        "8": "Rejected",
        "9": "Suspended",
        "A": "Pending New",
        "B": "Calculated",
        "C": "Expired",
        "D": "Restated",
        "E": "Pending Replace",
    },

    // Tag 167 - SecurityType (common values)
    "167": {
        "BA":     "Bankers Acceptance",
        "CB":     "Convertible Bond",
        "CD":     "Certificate of Deposit",
        "CMO":    "Collateralized Mortgage Obligation",
        "CORP":   "Corporate Bond",
        "CP":     "Commercial Paper",
        "CPP":    "Corp. Private Placement",
        "CS":     "Common Stock",
        "FHA":    "Federal Housing Authority",
        "FHL":    "Federal Home Loan",
        "FN":     "Federal National Mortgage Association",
        "FOR":    "Foreign Exchange Contract",
        "FUT":    "Future",
        "GN":     "Government National Mortgage Association",
        "GOVT":   "Treasuries + Agency Debenture",
        "IET":    "IOETTe Mortgage",
        "MF":     "Mutual Fund",
        "MIO":    "Mortgage Interest Only",
        "MPO":    "Mortgage Principal Only",
        "MPP":    "Mortgage Private Placement",
        "MPT":    "Miscellaneous Pass-through",
        "MUNI":   "Municipal Bond",
        "NONE":   "No Security Type",
        "OPT":    "Option",
        "PS":     "Preferred Stock",
        "RP":     "Repurchase Agreement",
        "RVRP":   "Reverse Repurchase Agreement",
        "SL":     "Student Loan Marketing Association",
        "TD":     "Time Deposit",
        "USTB":   "US Treasury Bill",
        "WAR":    "Warrant",
        "ZOO":    "Cats, Tigers & Lions",
        "?":      "Wildcard",
    },

    // Tag 373 - SessionRejectReason
    "373": {
        "0":  "Invalid Tag Number",
        "1":  "Required Tag Missing",
        "2":  "Tag Not Defined for This Message Type",
        "3":  "Undefined Tag",
        "4":  "Tag Specified Without a Value",
        "5":  "Value Is Incorrect (out of range) for This Tag",
        "6":  "Incorrect Data Format for Value",
        "7":  "Decryption Problem",
        "8":  "Signature Problem",
        "9":  "CompID Problem",
        "10": "SendingTime Accuracy Problem",
        "11": "Invalid MsgType",
    },

    // Tag 378 - ExecRestatementReason
    "378": {
        "0": "GT Corporate Action",
        "1": "GT Renewal/Restatement",
        "2": "Verbal Change",
        "3": "Repricing of Order",
        "4": "Broker Option",
        "5": "Partial Decline of OrderQty",
    },

    // Tag 394 - BidType
    "394": {
        "1": "Non Disclosed Style",
        "2": "Disclosed Style",
        "3": "No Bidding Process",
    },

    // Tag 434 - CxlRejResponseTo
    "434": {
        "1": "Order Cancel Request",
        "2": "Order Cancel/Replace Request",
    },
};

// ---------------------------------------------------------------------------
// 3. FIX_MSG_TYPES  --  MsgType code -> { name, category }
// ---------------------------------------------------------------------------

export const FIX_MSG_TYPES = {
    "0":  { name: "Heartbeat",                           category: "Session" },
    "1":  { name: "Test Request",                        category: "Session" },
    "2":  { name: "Resend Request",                      category: "Session" },
    "3":  { name: "Reject",                              category: "Session" },
    "4":  { name: "Sequence Reset",                      category: "Session" },
    "5":  { name: "Logout",                              category: "Session" },
    "6":  { name: "Indication of Interest",              category: "Pre-Trade" },
    "7":  { name: "Advertisement",                       category: "Pre-Trade" },
    "8":  { name: "Execution Report",                    category: "Trade" },
    "9":  { name: "Order Cancel Reject",                 category: "Trade" },
    "A":  { name: "Logon",                               category: "Session" },
    "B":  { name: "News",                                category: "Pre-Trade" },
    "C":  { name: "Email",                               category: "Pre-Trade" },
    "D":  { name: "New Order - Single",                  category: "Trade" },
    "E":  { name: "New Order - List",                    category: "Trade" },
    "F":  { name: "Order Cancel Request",                category: "Trade" },
    "G":  { name: "Order Cancel/Replace Request",        category: "Trade" },
    "H":  { name: "Order Status Request",                category: "Trade" },
    "J":  { name: "Allocation",                          category: "Post-Trade" },
    "K":  { name: "List Cancel Request",                 category: "Trade" },
    "L":  { name: "List Execute",                        category: "Trade" },
    "M":  { name: "List Status Request",                 category: "Trade" },
    "N":  { name: "List Status",                         category: "Trade" },
    "P":  { name: "Allocation ACK",                      category: "Post-Trade" },
    "Q":  { name: "Don't Know Trade",                    category: "Trade" },
    "R":  { name: "Quote Request",                       category: "Quoting" },
    "S":  { name: "Quote",                               category: "Quoting" },
    "T":  { name: "Settlement Instructions",             category: "Post-Trade" },
    "V":  { name: "Market Data Request",                 category: "Market Data" },
    "W":  { name: "Market Data - Snapshot/Full Refresh", category: "Market Data" },
    "X":  { name: "Market Data - Incremental Refresh",   category: "Market Data" },
    "Y":  { name: "Market Data Request Reject",          category: "Market Data" },
    "Z":  { name: "Quote Cancel",                        category: "Quoting" },
    "a":  { name: "Quote Status Request",                category: "Quoting" },
    "b":  { name: "Mass Quote Acknowledgement",          category: "Quoting" },
    "c":  { name: "Security Definition Request",         category: "Market Data" },
    "d":  { name: "Security Definition",                 category: "Market Data" },
    "e":  { name: "Security Status Request",             category: "Market Data" },
    "f":  { name: "Security Status",                     category: "Market Data" },
    "g":  { name: "Trading Session Status Request",      category: "Market Data" },
    "h":  { name: "Trading Session Status",              category: "Market Data" },
    "i":  { name: "Mass Quote",                          category: "Quoting" },
    "j":  { name: "Business Message Reject",             category: "Session" },
    "k":  { name: "Bid Request",                         category: "Trade" },
    "l":  { name: "Bid Response",                        category: "Trade" },
    "m":  { name: "List Strike Price",                   category: "Trade" },
};

// ---------------------------------------------------------------------------
// 4. FIX_HEADER_TAGS  --  tags that belong to the standard header
// ---------------------------------------------------------------------------

export const FIX_HEADER_TAGS = new Set([
    "8",   // BeginString
    "9",   // BodyLength
    "35",  // MsgType
    "49",  // SenderCompID
    "56",  // TargetCompID
    "115", // OnBehalfOfCompID
    "128", // DeliverToCompID
    "90",  // SecureDataLen
    "91",  // SecureData
    "34",  // MsgSeqNum
    "50",  // SenderSubID
    "142", // SenderLocationID
    "57",  // TargetSubID
    "143", // TargetLocationID
    "116", // OnBehalfOfSubID
    "144", // OnBehalfOfLocationID
    "129", // DeliverToSubID
    "145", // DeliverToLocationID
    "43",  // PossDupFlag
    "97",  // PossResend
    "52",  // SendingTime
    "122", // OrigSendingTime
    "212", // XmlDataLen
    "213", // XmlData
    "347", // MessageEncoding
    "369", // LastMsgSeqNumProcessed
    "370", // OnBehalfOfSendingTime
]);

// ---------------------------------------------------------------------------
// 5. FIX_TRAILER_TAGS  --  tags that belong to the trailer
// ---------------------------------------------------------------------------

export const FIX_TRAILER_TAGS = new Set([
    "10",  // CheckSum
    "89",  // Signature
    "93",  // SignatureLength
]);

// ---------------------------------------------------------------------------
// 6. Helper functions
// ---------------------------------------------------------------------------

/**
 * Returns the human-readable name for a FIX tag number.
 * Falls back to the tag number itself if unknown.
 *
 * @param {string|number} tag
 * @returns {string}
 */
export function translateTag(tag) {
    const key = String(tag);
    return FIX_TAGS[key] || key;
}

/**
 * Returns the human-readable label for an enum value on a given tag.
 * Falls back to the raw value if the tag has no enum mapping or the
 * value is not recognised.
 *
 * @param {string|number} tag
 * @param {string} value
 * @returns {string}
 */
export function translateEnum(tag, value) {
    const key = String(tag);
    const enumMap = FIX_ENUMS[key];
    if (enumMap) {
        const label = enumMap[value];
        if (label !== undefined) {
            return label;
        }
    }
    return value;
}

/**
 * Parse a raw FIX message string (tag=value delimited by | or SOH/\x01)
 * and return an array of field objects.
 *
 * Each object: { tag, name, value, translated, section }
 *   - tag:        the raw tag number (string)
 *   - name:       human-readable tag name
 *   - value:      the raw value string
 *   - translated: human-readable value (enum-translated when applicable)
 *   - section:    "header" | "body" | "trailer"
 *
 * @param {string} rawMessage
 * @returns {Array<{tag: string, name: string, value: string, translated: string, section: string}>}
 */
export function translateMessage(rawMessage) {
    if (!rawMessage || typeof rawMessage !== "string") {
        return [];
    }

    // Split on SOH (\x01) or pipe (|) -- common display delimiter
    const pairs = rawMessage.split(/\x01|\|/).filter(Boolean);
    const fields = [];

    for (const pair of pairs) {
        const eqIdx = pair.indexOf("=");
        if (eqIdx === -1) continue;

        const tag   = pair.substring(0, eqIdx);
        const value = pair.substring(eqIdx + 1);

        let section;
        if (FIX_HEADER_TAGS.has(tag)) {
            section = "header";
        } else if (FIX_TRAILER_TAGS.has(tag)) {
            section = "trailer";
        } else {
            section = "body";
        }

        fields.push({
            tag,
            name:       translateTag(tag),
            value,
            translated: translateEnum(tag, value),
            section,
        });
    }

    return fields;
}

/**
 * Given a parsed fields object (a Map or plain object keyed by tag number,
 * or an array of {tag, value} objects), produce a one-line human-readable
 * order summary.
 *
 * Example output: "Buy 100 AAPL @ 150.00 Limit Day"
 *
 * @param {Map|Object|Array} fields
 * @returns {string}
 */
export function formatOrderSummary(fields) {
    // Normalise input into a simple tag->value lookup
    let lookup;
    if (fields instanceof Map) {
        lookup = (tag) => fields.get(String(tag));
    } else if (Array.isArray(fields)) {
        const map = new Map();
        for (const f of fields) {
            map.set(String(f.tag), f.value);
        }
        lookup = (tag) => map.get(String(tag));
    } else if (fields && typeof fields === "object") {
        lookup = (tag) => fields[String(tag)];
    } else {
        return "";
    }

    const parts = [];

    // Side (tag 54)
    const side = lookup("54");
    if (side) {
        parts.push(translateEnum("54", side));
    }

    // Quantity (tag 38)
    const qty = lookup("38");
    if (qty) {
        parts.push(qty);
    }

    // Symbol (tag 55)
    const symbol = lookup("55");
    if (symbol) {
        parts.push(symbol);
    }

    // Price (tag 44) -- prefixed with @
    const price = lookup("44");
    if (price) {
        parts.push("@");
        parts.push(price);
    }

    // StopPx (tag 99)
    const stopPx = lookup("99");
    if (stopPx) {
        parts.push("stop");
        parts.push(stopPx);
    }

    // OrdType (tag 40)
    const ordType = lookup("40");
    if (ordType) {
        parts.push(translateEnum("40", ordType));
    }

    // TimeInForce (tag 59)
    const tif = lookup("59");
    if (tif) {
        // Use short form for common TIF values
        const tifShort = {
            "0": "Day",
            "1": "GTC",
            "2": "OPG",
            "3": "IOC",
            "4": "FOK",
            "5": "GTX",
            "6": "GTD",
        };
        parts.push(tifShort[tif] || translateEnum("59", tif));
    }

    return parts.join(" ");
}

/**
 * Returns the human-readable name for a MsgType code.
 * Falls back to "Unknown (<code>)" if not recognised.
 *
 * @param {string} code
 * @returns {string}
 */
export function msgTypeName(code) {
    const entry = FIX_MSG_TYPES[code];
    return entry ? entry.name : `Unknown (${code})`;
}
