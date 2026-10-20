/*====================================================================
  Program : xpt2sas7bdat.sas
  Purpose : Convert every SAS XPORT (V5 *.xpt) file under &indir into
            native sas7bdat datasets, producing two copies:
                &outdir  -> UTF-8    encoding
                &cndir   -> EUC-CN   encoding (Simplified Chinese / GBK)
            Directory layout sdtmdata/ adamdata/ is preserved.

  IMPORTANT - run in a WLATIN1 (Windows-1252) session:
            The source XPORT files carry Windows-1252 bytes (e.g. 0x92
            right single quote in ts.xpt). A UTF-8 session would reject /
            corrupt them, so start SAS with -ENCODING WLATIN1 and let SAS
            transcode on output via the ENCODING= data set option.

  Usage   : sas.exe -sysin xpt2sas7bdat.sas -encoding wlatin1
                    -log xpt2sas7bdat.log
                    -sysparm "indir|outdir|cndir|subset"
            indir  : folder searched recursively for *.xpt   (default tests)
            outdir : UTF-8  output library root
            cndir  : EUC-CN output library root
            subset : optional, blank-separated dataset names to limit the
                     run, e.g. adsl dm  (blank = convert all)
====================================================================*/
options dlcreatedir noxwait noxsync nonotes;

%let indir  = d:\developai\mcp\sas-local-mcp\tests\xpt;
%let outdir = d:\developai\mcp\sas-local-mcp\tests\sasdata;
%let cndir  = d:\developai\mcp\sas-local-mcp\tests\sasdata_cn;
%let subset = ;

/* ---- override defaults from -sysparm ------------------------------ */
%macro _getparms;
  %global indir outdir cndir subset;
  %local p;
  %let p = %sysfunc(getoption(sysparm));
  %if %length(%superq(p)) > 0 %then %do;
    %let indir  = %scan(%superq(p),1,|);
    %let outdir = %scan(%superq(p),2,|);
    %let cndir  = %scan(%superq(p),3,|);
    %let subset = %scan(%superq(p),4,|);
  %end;
  %put XPT2SAS7BDAT: indir  = &indir;
  %put XPT2SAS7BDAT: outdir = &outdir  (UTF-8);
  %put XPT2SAS7BDAT: cndir  = &cndir   (EUC-CN);
  %put XPT2SAS7BDAT: subset = &subset;
%mend _getparms;
%_getparms;

/* ---- list the xpt files ------------------------------------------- */
filename _xlst pipe %sysfunc(quote(dir /b /s "&indir.\*.xpt"));

data _files;
  length path $512 subdir $32 ds $32;
  infile _xlst truncover;
  input path $char512.;
  path = strip(path);
  if path = '' then delete;
  if upcase(scan(path,-1,'.')) ne 'XPT' then delete;
  subdir = scan(path,-2,'\');
  ds     = upcase(scan(scan(path,-1,'\'),1,'.'));
run;

filename _xlst clear;

%if %length(%superq(subset)) > 0 %then %do;
  data _files;
    set _files;
    if indexw(upcase("&subset"), strip(ds)) = 0 then delete;
  run;
%end;

proc sql noprint;
  select count(*) into :_nfile from _files;
quit;
%put XPT2SAS7BDAT: xpt files to convert = &_nfile;

/* ---- create the two library roots (DLCREATEDIR only makes the leaf) */
libname rt8 "&outdir";
libname rtc "&cndir";
%if &syslibrc ne 0 %then %do;
  %put ERROR: cannot create library root &cndir;
%end;
libname rt8 clear;
libname rtc clear;

/* ---- conversion macro --------------------------------------------- */
%macro xpt2sas7bdat(path=, subdir=, ds=);
  %local mems i m n mlbl nchar;

  libname xpin xport "&path";
  %if &syslibrc ne 0 %then %do;
    %put ERROR: cannot open XPORT file &path;
    %goto done;
  %end;

  proc sql noprint;
    select memname into :mems separated by '|'
    from dictionary.tables
    where libname = 'XPIN' and memtype = 'DATA';
  quit;
  %let n = &sqlobs;
  %if &n = 0 %then %do;
    %put WARNING: no data member found in &path;
    %goto done;
  %end;

  libname u8out "&outdir.\&subdir";
  libname cnout "&cndir.\&subdir";
  %if &syslibrc ne 0 %then %do;
    %put ERROR: cannot assign output library &cndir.\&subdir;
    %goto done;
  %end;

  %do i = 1 %to &n;
    %let m = %scan(&mems,&i,|);

    /* data set label + number of character variables */
    proc sql noprint;
      select memlabel into :mlbl
      from dictionary.tables
      where libname = 'XPIN' and memname = "&m";

      select count(*) into :nchar
      from dictionary.columns
      where libname = 'XPIN' and memname = "&m" and type = 'char';
    quit;

    data u8out.&m(encoding='UTF-8'  label=%sysfunc(quote(%superq(mlbl))))
         cnout.&m(encoding='EUC-CN' label=%sysfunc(quote(%superq(mlbl))));
      set xpin.&m;
      %if &nchar > 0 %then %do;
        /* cp1252 punctuation (0x91-0x94,0x96,0x97) -> safe ASCII,
           otherwise EUC-CN transcoding fails (e.g. Alzheimer<92>s) */
        array _cv _character_;
        do _j = 1 to dim(_cv);
          _cv(_j) = translate(_cv(_j), '602722222D2D'x, '919293949697'x);
        end;
        drop _j;
      %end;
    run;

    %put XPT2SAS7BDAT: converted &subdir\&ds..xpt -> &m (UTF-8 + EUC-CN);
  %end;

  libname u8out clear;
  libname cnout clear;

  %done:
  libname xpin clear;
%mend xpt2sas7bdat;

/* ---- driver -------------------------------------------------------- */
%macro _run_all;
  %local i p s d;
  %do i = 1 %to &_nfile;
    data _null_;
      set _files(firstobs=&i obs=&i);
      call symputx('p', path);
      call symputx('s', subdir);
      call symputx('d', ds);
    run;
    %xpt2sas7bdat(path=&p, subdir=&s, ds=&d);
  %end;
%mend _run_all;
%_run_all;

proc datasets lib=work nolist;
  delete _files;
run;
quit;
