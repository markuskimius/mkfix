/**
 * FIX message formatting utilities for the translated message viewer.
 */

import { FIX_ENUMS, translateTag, translateEnum, msgTypeName } from "./fix-dictionary.js";

/**
 * Build a one-line summary for any FIX message based on its type.
 * E.g., "NewOrderSingle: Buy 100 AAPL @ 150.00 Limit Day"
 */
export function summarizeMessage(fields) {
  const f = (tag) => fields[tag] || "";
  const e = (tag) => translateEnum(tag, fields[tag] || "");
  const msgType = f("35");
  const name = msgTypeName(msgType);

  switch (msgType) {
    case "D": // NewOrderSingle
      return `${name}: ${e("54")} ${f("38")} ${f("55")} @ ${f("44") || "MKT"} ${e("40")} ${e("59")}`;
    case "F": // OrderCancelRequest
      return `${name}: ${e("54")} ${f("55")} OrigClOrdID=${f("41")}`;
    case "G": // OrderCancelReplaceRequest
      return `${name}: ${e("54")} ${f("38")} ${f("55")} @ ${f("44") || "MKT"} OrigClOrdID=${f("41")}`;
    case "8": { // ExecutionReport
      const execType = e("150");
      const status = e("39");
      const lastQty = f("32");
      const lastPx = f("31");
      const fill = lastQty && lastPx ? ` ${lastQty}@${lastPx}` : "";
      return `${name}: ${e("54")} ${f("55")} ${status}${fill} ClOrdID=${f("11")}`;
    }
    case "9": // OrderCancelReject
      return `${name}: ClOrdID=${f("11")} ${e("102")}`;
    case "A": // Logon
      return `${name}: HeartBtInt=${f("108")}${f("141") === "Y" ? " ResetSeqNum" : ""}`;
    case "5": // Logout
      return `${name}${f("58") ? ": " + f("58") : ""}`;
    case "0": // Heartbeat
      return `${name}${f("112") ? " TestReqID=" + f("112") : ""}`;
    case "1": // TestRequest
      return `${name}: TestReqID=${f("112")}`;
    case "2": // ResendRequest
      return `${name}: ${f("7")}-${f("16")}`;
    case "4": // SequenceReset
      return `${name}: NewSeqNo=${f("36")}${f("123") === "Y" ? " GapFill" : ""}`;
    case "3": // Reject
      return `${name}: RefSeqNum=${f("45")} ${f("58") || ""}`;
    case "6": // IOI
      return `${name}: ${e("28")} ${e("54")} ${f("55")} Qty=${e("27")}`;
    case "7": // Advertisement
      return `${name}: ${e("5")} ${e("4")} ${f("55")}`;
    case "E": // NewOrderList
      return `${name}: ListID=${f("66")} Orders=${f("68")}`;
    case "J": // Allocation
      return `${name}: ${e("71")} AllocID=${f("70")} ${f("55")}`;
    case "P": // AllocationACK
      return `${name}: AllocID=${f("70")} ${e("87")}`;
    default:
      return `${name}`;
  }
}

/**
 * Parse a raw FIX message string into a structured field list.
 * Returns {fields: {tag: value}, fieldList: [{tag, value}]}
 */
export function parseRawMessage(raw) {
  const sep = raw.includes("\x01") ? "\x01" : "|";
  const fields = {};
  const fieldList = [];
  for (const pair of raw.split(sep)) {
    if (!pair) continue;
    const eq = pair.indexOf("=");
    if (eq > 0) {
      const tag = pair.substring(0, eq);
      const value = pair.substring(eq + 1);
      fields[tag] = value;
      fieldList.push({ tag, value });
    }
  }
  return { fields, fieldList };
}
