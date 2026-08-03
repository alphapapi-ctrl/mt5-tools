//+------------------------------------------------------------------+
//| ReportExporter.mq5                                               |
//| Exports closed-trade history as an HTML report to a local folder |
//| on a refresh interval. Replacement for the removed MT5 FTP       |
//| "Publisher" feature. Output format matches the MT5 Account       |
//| History table parsed by MT5Tools/mt5_parser.py (parse_mt5_report)|
//|                                                                  |
//| Resource usage: no chart drawing, no tick processing. Runs only  |
//| on a timer (and optionally on trade events) and skips the export |
//| entirely when no new deals have appeared.                        |
//|                                                                  |
//| Output location (MT5 file sandbox):                              |
//|   InpUseCommonFolder = true  ->                                  |
//|     %APPDATA%\MetaQuotes\Terminal\Common\Files\<InpSubFolder>\   |
//|   InpUseCommonFolder = false ->                                  |
//|     <TerminalData>\MQL5\Files\<InpSubFolder>\                    |
//+------------------------------------------------------------------+
#property copyright "MT5Tools"
#property version   "1.00"
#property strict

//--- inputs
input int    InpRefreshSeconds   = 300;    // Refresh interval (seconds)
input bool   InpUseCommonFolder  = true;   // Write to Common\Files (shared across terminals)
input string InpSubFolder        = "Reports"; // Sub-folder inside Files ("" = none)
input bool   InpAccountSubfolder = true;   // Add per-account subfolder (matches FTP layout /<account>/)
input string InpFileName         = "";     // File name ("" = <account>.htm)
input bool   InpExportOnTrade    = true;   // Also export immediately when a deal closes
input int    InpHistoryDays      = 0;      // History depth in days (0 = full history)

//--- change-detection state
int    g_lastDealsTotal = -1;
ulong  g_lastDealTicket = 0;
double g_lastBalance    = -1.0;

//--- one reconstructed position (one row in the report)
struct PositionRow
  {
   long              pos_id;
   string            symbol;
   string            type;       // "buy" / "sell"
   string            comment;
   datetime          open_time;
   datetime          close_time;
   double            volume;     // total entry volume
   double            in_vol;     // accumulated for weighted averages
   double            out_vol;
   double            open_price; // volume-weighted
   double            close_price;// volume-weighted
   double            sl;
   double            tp;
   double            commission;
   double            swap;
   double            profit;
   int               digits;
   bool              closed;
  };

//+------------------------------------------------------------------+
int OnInit()
  {
   EventSetTimer(MathMax(InpRefreshSeconds, 5));
   ExportReport(true);
   return(INIT_SUCCEEDED);
  }
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   EventKillTimer();
  }
//+------------------------------------------------------------------+
void OnTimer()
  {
   ExportReport(false);
  }
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
  {
   if(InpExportOnTrade && trans.type == TRADE_TRANSACTION_DEAL_ADD)
      ExportReport(false);
  }
//+------------------------------------------------------------------+
//| Build file name / relative path inside the Files sandbox         |
//+------------------------------------------------------------------+
string ReportPath()
  {
   string acc  = IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN));
   string name = InpFileName;
   if(name == "")
      name = acc + ".htm";
   string path = name;
   if(InpAccountSubfolder)
      path = acc + "\\" + path;
   if(InpSubFolder != "")
      path = InpSubFolder + "\\" + path;
   return(path);
  }
//+------------------------------------------------------------------+
//| Export the report. force=true skips the no-new-deals shortcut.   |
//+------------------------------------------------------------------+
void ExportReport(bool force)
  {
   datetime from = 0;
   if(InpHistoryDays > 0)
      from = TimeCurrent() - (datetime)InpHistoryDays * 86400;

   if(!HistorySelect(from, TimeCurrent() + 86400))
     {
      Print("ReportExporter: HistorySelect failed");
      return;
     }

   int total = HistoryDealsTotal();
   ulong lastTicket = (total > 0) ? HistoryDealGetTicket(total - 1) : 0;

   // Skip if nothing changed since last export. Open positions change price
   // continuously, so the shortcut only applies when the account is flat.
   // Balance is part of the change key: it catches deposits/withdrawals and,
   // more importantly, the first export after startup running before account
   // data has synced (balance reads 0) — the next timer tick re-exports.
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   if(!force && PositionsTotal() == 0 &&
      total == g_lastDealsTotal && lastTicket == g_lastDealTicket &&
      MathAbs(balance - g_lastBalance) < 0.005)
      return;

   PositionRow rows[];
   int nRows = 0;

   for(int i = 0; i < total; i++)
     {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0)
         continue;

      long dtype = HistoryDealGetInteger(ticket, DEAL_TYPE);
      if(dtype != DEAL_TYPE_BUY && dtype != DEAL_TYPE_SELL)
         continue; // skip balance, credit, corrections, etc.

      long pos_id = HistoryDealGetInteger(ticket, DEAL_POSITION_ID);
      if(pos_id == 0)
         continue;

      long   entry  = HistoryDealGetInteger(ticket, DEAL_ENTRY);
      double vol    = HistoryDealGetDouble(ticket, DEAL_VOLUME);
      double price  = HistoryDealGetDouble(ticket, DEAL_PRICE);
      datetime dtime= (datetime)HistoryDealGetInteger(ticket, DEAL_TIME);

      // find (or create) the row for this position — search backwards,
      // recent deals almost always belong to recent positions
      int idx = -1;
      for(int j = nRows - 1; j >= 0; j--)
         if(rows[j].pos_id == pos_id) { idx = j; break; }

      if(idx < 0)
        {
         ArrayResize(rows, nRows + 1, 256);
         idx = nRows++;
         PositionRow r;
         ZeroMemory(r);
         r.pos_id  = pos_id;
         r.symbol  = HistoryDealGetString(ticket, DEAL_SYMBOL);
         r.digits  = (int)SymbolInfoInteger(r.symbol, SYMBOL_DIGITS);
         if(r.digits <= 0) r.digits = 5;
         r.open_time = dtime;
         rows[idx] = r;
        }

      rows[idx].commission += HistoryDealGetDouble(ticket, DEAL_COMMISSION);
      rows[idx].swap       += HistoryDealGetDouble(ticket, DEAL_SWAP);
      rows[idx].profit     += HistoryDealGetDouble(ticket, DEAL_PROFIT);

      if(entry == DEAL_ENTRY_IN)
        {
         if(rows[idx].in_vol == 0.0)
           {
            rows[idx].type      = (dtype == DEAL_TYPE_BUY) ? "buy" : "sell";
            rows[idx].comment   = HistoryDealGetString(ticket, DEAL_COMMENT);
            rows[idx].open_time = dtime;
           }
         rows[idx].open_price = (rows[idx].open_price * rows[idx].in_vol + price * vol)
                                / (rows[idx].in_vol + vol);
         rows[idx].in_vol += vol;
         rows[idx].volume  = rows[idx].in_vol;
        }
      else // OUT, OUT_BY, INOUT — closing leg
        {
         rows[idx].close_price = (rows[idx].out_vol == 0.0)
                                 ? price
                                 : (rows[idx].close_price * rows[idx].out_vol + price * vol)
                                   / (rows[idx].out_vol + vol);
         rows[idx].out_vol   += vol;
         rows[idx].close_time = dtime;
         rows[idx].sl = HistoryDealGetDouble(ticket, DEAL_SL);
         rows[idx].tp = HistoryDealGetDouble(ticket, DEAL_TP);
         if(rows[idx].out_vol >= rows[idx].in_vol - 0.0000001)
            rows[idx].closed = true;
        }
     }

   //--- write to a temp file, then atomically swap in, so readers
   //--- never see a half-written report
   int common = InpUseCommonFolder ? FILE_COMMON : 0;
   string path = ReportPath();
   string tmp  = path + ".tmp";

   int fh = FileOpen(tmp, FILE_WRITE | FILE_TXT | FILE_ANSI | common);
   if(fh == INVALID_HANDLE)
     {
      Print("ReportExporter: cannot open ", tmp, " err=", GetLastError());
      return;
     }

   string acc = IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN));
   FileWriteString(fh, "<html><head><meta charset=\"utf-8\">");
   FileWriteString(fh, "<title>Trade History Report " + acc + "</title></head><body>\n");
   FileWriteString(fh, "<h1>Trade History Report</h1>\n");
   FileWriteString(fh, "<p>Account: " + acc + " (" +
                       AccountInfoString(ACCOUNT_COMPANY) + ", " +
                       AccountInfoString(ACCOUNT_CURRENCY) + ")<br>\n");
   FileWriteString(fh, "Generated: " + TimeToString(TimeCurrent(), TIME_DATE | TIME_SECONDS) + "</p>\n");
   FileWriteString(fh, "<table border=\"1\" cellspacing=\"0\">\n");
   FileWriteString(fh, "<tr><th>Time</th><th>Position</th><th>Symbol</th><th>Type</th>"
                       "<th>Comment</th><th>Volume</th><th>Price</th><th>S/L</th><th>T/P</th>"
                       "<th>Time</th><th>Price</th><th>Commission</th><th>Swap</th><th>Profit</th></tr>\n");

   double totalProfit = 0.0;
   int nClosed = 0;
   for(int i = 0; i < nRows; i++)
     {
      if(!rows[i].closed)
         continue; // report closed positions only
      nClosed++;
      int d = rows[i].digits;
      totalProfit += rows[i].profit + rows[i].commission + rows[i].swap;
      FileWriteString(fh, "<tr>"
         "<td>" + TimeToString(rows[i].open_time, TIME_DATE | TIME_SECONDS) + "</td>"
         "<td>" + IntegerToString(rows[i].pos_id) + "</td>"
         "<td>" + rows[i].symbol + "</td>"
         "<td>" + rows[i].type + "</td>"
         "<td>" + rows[i].comment + "</td>"
         "<td>" + DoubleToString(rows[i].volume, 2) + "</td>"
         "<td>" + DoubleToString(rows[i].open_price, d) + "</td>"
         "<td>" + DoubleToString(rows[i].sl, d) + "</td>"
         "<td>" + DoubleToString(rows[i].tp, d) + "</td>"
         "<td>" + TimeToString(rows[i].close_time, TIME_DATE | TIME_SECONDS) + "</td>"
         "<td>" + DoubleToString(rows[i].close_price, d) + "</td>"
         "<td>" + DoubleToString(rows[i].commission, 2) + "</td>"
         "<td>" + DoubleToString(rows[i].swap, 2) + "</td>"
         "<td>" + DoubleToString(rows[i].profit, 2) + "</td>"
         "</tr>\n");
     }

   FileWriteString(fh, "<tr><td>Total Net Profit: " + DoubleToString(totalProfit, 2) + "</td></tr>\n");
   FileWriteString(fh, "</table>\n");

   //--- open positions section (matches parse_open_positions in mt5_parser.py)
   int nOpen = PositionsTotal();
   if(nOpen > 0)
     {
      FileWriteString(fh, "<table border=\"1\" cellspacing=\"0\">\n");
      FileWriteString(fh, "<tr><td colspan=\"12\"><b>Open Positions</b></td></tr>\n");
      FileWriteString(fh, "<tr><th>Time</th><th>Position</th><th>Symbol</th><th>Type</th>"
                          "<th>Volume</th><th>Price</th><th>S/L</th><th>T/P</th>"
                          "<th>Market Price</th><th>Swap</th><th>Profit</th><th>Comment</th></tr>\n");
      for(int i = 0; i < nOpen; i++)
        {
         ulong ptk = PositionGetTicket(i);
         if(ptk == 0)
            continue;
         string psym = PositionGetString(POSITION_SYMBOL);
         int pd = (int)SymbolInfoInteger(psym, SYMBOL_DIGITS);
         if(pd <= 0) pd = 5;
         FileWriteString(fh, "<tr>"
            "<td>" + TimeToString((datetime)PositionGetInteger(POSITION_TIME), TIME_DATE | TIME_SECONDS) + "</td>"
            "<td>" + IntegerToString(PositionGetInteger(POSITION_IDENTIFIER)) + "</td>"
            "<td>" + psym + "</td>"
            "<td>" + ((PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? "buy" : "sell") + "</td>"
            "<td>" + DoubleToString(PositionGetDouble(POSITION_VOLUME), 2) + "</td>"
            "<td>" + DoubleToString(PositionGetDouble(POSITION_PRICE_OPEN), pd) + "</td>"
            "<td>" + DoubleToString(PositionGetDouble(POSITION_SL), pd) + "</td>"
            "<td>" + DoubleToString(PositionGetDouble(POSITION_TP), pd) + "</td>"
            "<td>" + DoubleToString(PositionGetDouble(POSITION_PRICE_CURRENT), pd) + "</td>"
            "<td>" + DoubleToString(PositionGetDouble(POSITION_SWAP), 2) + "</td>"
            "<td>" + DoubleToString(PositionGetDouble(POSITION_PROFIT), 2) + "</td>"
            "<td>" + PositionGetString(POSITION_COMMENT) + "</td>"
            "</tr>\n");
        }
      FileWriteString(fh, "</table>\n");
     }

   FileWriteString(fh, "<p>Balance: " + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2) +
                       " &nbsp; Equity: " + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2) + "</p>\n");
   FileWriteString(fh, "</body></html>\n");
   FileClose(fh);

   if(!FileMove(tmp, common, path, FILE_REWRITE | common))
     {
      Print("ReportExporter: FileMove failed err=", GetLastError());
      FileDelete(tmp, common);
      return;
     }

   g_lastDealsTotal = total;
   g_lastDealTicket = lastTicket;
   g_lastBalance    = balance;
   PrintFormat("ReportExporter: exported %d closed positions to %s",
               nClosed, path);
  }
//+------------------------------------------------------------------+
